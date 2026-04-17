"""Back-compat Google integration shim over the generic accounts store.

Old Home + Settings code paths want:

  GET /integrations/status                       {google: {system, user}}
  GET /integrations/google/{role}/inbox/glance   24h unread summary

Both now read from `AccountsStore` under the hood. New code paths use
`/integrations/accounts*` directly; this file stays so the UI doesn't
break mid-refactor.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from core.identity import AccountBinding, AccountsStore
from core.integrations.google.oauth import credentials_from_blob
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
