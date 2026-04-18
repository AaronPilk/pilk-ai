"""Back-compat Google integration shim over the generic accounts store.

Old Home + Settings code paths want:

  GET /integrations/status                          {google: {system, user}}
  GET /integrations/google/{role}/inbox/glance      24h unread summary
  GET /integrations/google/{role}/calendar/glance   today's events summary

Both glance routes read from `AccountsStore` under the hood. New code
paths use `/integrations/accounts*` directly; this file stays so the
Home tiles don't have to route through the planning/approval queue for
read-only at-a-glance views.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta

from fastapi import APIRouter, HTTPException, Request

from core.identity import AccountBinding, AccountsStore
from core.integrations.google.oauth import credentials_from_blob
from core.integrations.providers.google import SCOPE_CATALOG
from core.logging import get_logger

log = get_logger("pilkd.integrations")

router = APIRouter(prefix="/integrations")


def _store(request: Request) -> AccountsStore:
    store = getattr(request.app.state, "accounts", None)
    if store is None:
        raise HTTPException(status_code=503, detail="accounts store offline")
    return store


ROLE_LABELS: dict[str, str] = {
    "system": "PILK operational mail",
    "user": "Your working mail",
}


@router.get("/status")
async def integrations_status(request: Request) -> dict:
    store = _store(request)
    google: dict = {}
    for role in ("system", "user"):
        account = store.default("google", role)  # type: ignore[arg-type]
        if account is not None:
            google[role] = {
                "linked": True,
                "email": account.email,
                "scopes": list(account.scopes),
                "linked_at": account.linked_at,
                "error": None,
                "role": role,
                "label": ROLE_LABELS[role],
                "account_id": account.account_id,
            }
        else:
            google[role] = {
                "linked": False,
                "email": None,
                "scopes": [],
                "linked_at": None,
                "error": None,
                "role": role,
                "label": ROLE_LABELS[role],
                "account_id": None,
            }
    return {"google": google}


# ── Gmail glance ────────────────────────────────────────────────────────

INBOX_GLANCE_QUERY = "is:unread newer_than:1d in:inbox"
INBOX_GLANCE_PREVIEW = 3
INBOX_GLANCE_MAX = 25


@router.get("/google/{role}/inbox/glance")
async def google_inbox_glance(role: str, request: Request) -> dict:
    if role not in ("system", "user"):
        raise HTTPException(status_code=400, detail=f"unknown role: {role}")
    store = _store(request)
    binding = AccountBinding(provider="google", role=role)
    account = store.resolve_binding(binding)
    if account is None:
        return {
            "linked": False,
            "email": None,
            "unread": 0,
            "preview": [],
            "role": role,
        }
    tokens = store.load_tokens(account.account_id)
    if tokens is None:
        return {
            "linked": False,
            "email": account.email,
            "unread": 0,
            "preview": [],
            "role": role,
            "error": "tokens missing on disk; try re-linking",
        }
    creds = credentials_from_blob(
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "client_id": tokens.client_id,
            "client_secret": tokens.client_secret,
            "scopes": tokens.scopes,
            "token_uri": tokens.token_uri,
            "email": account.email,
        }
    )
    if creds is None:
        return {
            "linked": True,
            "email": account.email,
            "unread": 0,
            "preview": [],
            "role": role,
            "error": "couldn't build Google credentials",
        }
    try:
        result = await asyncio.to_thread(_inbox_glance_sync, creds)
    except Exception as e:
        log.exception("inbox_glance_failed", role=role, account_id=account.account_id)
        return {
            "linked": True,
            "email": account.email,
            "unread": 0,
            "preview": [],
            "role": role,
            "error": f"{type(e).__name__}: {e}",
        }
    return {
        "linked": True,
        "email": account.email,
        "unread": result["unread"],
        "preview": result["preview"],
        "role": role,
    }


# ── Calendar glance ─────────────────────────────────────────────────────

CALENDAR_GLANCE_PREVIEW = 4
CALENDAR_READ_SCOPE = SCOPE_CATALOG["calendar.readonly"].scope_uri


@router.get("/google/{role}/calendar/glance")
async def google_calendar_glance(role: str, request: Request) -> dict:
    """Cheap today's-events summary for the Home command center.

    Returns `{linked, events_count, preview}` when the calendar scope is
    enabled on the connected account. If the account is linked but the
    `calendar` scope group wasn't added at OAuth time, responds with
    `scope_missing: true` so the Home tile can surface the "Expand
    access — add Calendar" CTA.
    """
    if role not in ("system", "user"):
        raise HTTPException(status_code=400, detail=f"unknown role: {role}")
    store = _store(request)
    binding = AccountBinding(provider="google", role=role)
    account = store.resolve_binding(binding)
    if account is None:
        return {
            "linked": False,
            "email": None,
            "events_count": 0,
            "preview": [],
            "role": role,
        }
    if CALENDAR_READ_SCOPE not in (account.scopes or []):
        return {
            "linked": True,
            "email": account.email,
            "events_count": 0,
            "preview": [],
            "role": role,
            "scope_missing": True,
        }
    tokens = store.load_tokens(account.account_id)
    if tokens is None:
        return {
            "linked": False,
            "email": account.email,
            "events_count": 0,
            "preview": [],
            "role": role,
            "error": "tokens missing on disk; try re-linking",
        }
    creds = credentials_from_blob(
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "client_id": tokens.client_id,
            "client_secret": tokens.client_secret,
            "scopes": tokens.scopes,
            "token_uri": tokens.token_uri,
            "email": account.email,
        }
    )
    if creds is None:
        return {
            "linked": True,
            "email": account.email,
            "events_count": 0,
            "preview": [],
            "role": role,
            "error": "couldn't build Google credentials",
        }
    try:
        result = await asyncio.to_thread(_calendar_glance_sync, creds)
    except Exception as e:
        log.exception("calendar_glance_failed", role=role)
        return {
            "linked": True,
            "email": account.email,
            "events_count": 0,
            "preview": [],
            "role": role,
            "error": f"{type(e).__name__}: {e}",
        }
    return {
        "linked": True,
        "email": account.email,
        "events_count": result["events_count"],
        "preview": result["preview"],
        "role": role,
    }


def _calendar_glance_sync(creds) -> dict:
    service = creds.build("calendar", "v3")
    day_start = datetime.combine(
        datetime.now(UTC).date(), time.min, tzinfo=UTC
    )
    day_end = day_start + timedelta(days=1)
    listing = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=25,
        )
        .execute()
    )
    items = listing.get("items") or []
    preview: list[dict] = []
    for e in items[:CALENDAR_GLANCE_PREVIEW]:
        preview.append(
            {
                "summary": e.get("summary", "(no title)"),
                "start": _event_time(e.get("start")),
                "end": _event_time(e.get("end")),
            }
        )
    return {"events_count": len(items), "preview": preview}


def _event_time(block) -> str:
    if not block:
        return ""
    return block.get("dateTime") or block.get("date") or ""


def _inbox_glance_sync(creds) -> dict:
    service = creds.build("gmail", "v1")
    listing = (
        service.users()
        .messages()
        .list(userId="me", q=INBOX_GLANCE_QUERY, maxResults=INBOX_GLANCE_MAX)
        .execute()
    )
    ids = [m["id"] for m in (listing.get("messages") or [])]
    preview: list[dict] = []
    for mid in ids[:INBOX_GLANCE_PREVIEW]:
        try:
            m = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                )
                .execute()
            )
        except Exception:
            continue
        headers = {
            h["name"]: h.get("value", "")
            for h in (m.get("payload") or {}).get("headers") or []
        }
        preview.append(
            {
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", "(no subject)"),
                "received_at": headers.get("Date", ""),
            }
        )
    return {"unread": len(ids), "preview": preview}
