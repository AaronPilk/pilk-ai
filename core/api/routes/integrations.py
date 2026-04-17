"""Integrations HTTP surface.

  GET /integrations/status                          which accounts linked
  GET /integrations/google/{role}/inbox/glance      24h unread summary

Read-only surface. Write actions (send, reply, archive) go through the
tool gateway + approval queue — never through this router.

A single Google integration has two roles:
  - system: PILK's operational mail (sends reports to you, handles
            developer-account signups, etc.)
  - user:   your real working inbox (triage, drafting replies)
Each role has its own OAuth blob; see core/integrations/google/accounts.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from core.config import get_settings
from core.integrations.google import (
    ROLE_LABELS,
    ROLES,
    google_status,
    load_credentials,
)
from core.logging import get_logger

log = get_logger("pilkd.integrations")

router = APIRouter(prefix="/integrations")


@router.get("/status")
async def integrations_status(request: Request) -> dict:
    settings = get_settings()
    google: dict = {}
    for role in ROLES:
        status = google_status(settings.google_role_path(role))
        google[role] = {
            **status.to_public(),
            "role": role,
            "label": ROLE_LABELS[role],
        }
    return {"google": google}


# ── Gmail glance ────────────────────────────────────────────────────────

INBOX_GLANCE_QUERY = "is:unread newer_than:1d in:inbox"
INBOX_GLANCE_PREVIEW = 3
INBOX_GLANCE_MAX = 25


@router.get("/google/{role}/inbox/glance")
async def google_inbox_glance(role: str) -> dict:
    """Cheap unread-today summary for the Home command center.

    Returns unread count + a handful of senders/subjects, nothing
    expensive. Role must be "system" or "user"; Home calls this with
    `user` so the tile reflects the real inbox, not the operational
    mailbox.
    """
    if role not in ROLES:
        raise HTTPException(status_code=400, detail=f"unknown role: {role}")
    settings = get_settings()
    creds = load_credentials(settings.google_role_path(role))
    if creds is None:
        return {
            "linked": False,
            "email": None,
            "unread": 0,
            "preview": [],
            "role": role,
        }
    try:
        result = await asyncio.to_thread(_inbox_glance_sync, creds)
    except Exception as e:
        log.exception("inbox_glance_failed", role=role)
        return {
            "linked": True,
            "email": creds.email,
            "unread": 0,
            "preview": [],
            "role": role,
            "error": f"{type(e).__name__}: {e}",
        }
    return {
        "linked": True,
        "email": creds.email,
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
