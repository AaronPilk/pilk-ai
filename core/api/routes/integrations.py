"""Integrations HTTP surface.

  GET /integrations/status               which external accounts are linked
  GET /integrations/google/inbox/glance  last-24h unread summary for Home

Read-only surface. Write actions (send, reply, archive) go through the
tool gateway + approval queue — never through this router.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from core.config import get_settings
from core.integrations.google import google_status, load_credentials
from core.logging import get_logger

log = get_logger("pilkd.integrations")

router = APIRouter(prefix="/integrations")


@router.get("/status")
async def integrations_status(request: Request) -> dict:
    settings = get_settings()
    google = google_status(settings.google_credentials_path)
    return {
        "google": google.to_public(),
    }


# ── Gmail glance ────────────────────────────────────────────────────────

INBOX_GLANCE_QUERY = "is:unread newer_than:1d in:inbox"
INBOX_GLANCE_PREVIEW = 3
INBOX_GLANCE_MAX = 25


@router.get("/google/inbox/glance")
async def google_inbox_glance() -> dict:
    """Cheap unread-today summary for the Home command center.

    Returns unread count + a handful of senders/subjects, nothing
    expensive. No body content, no ids surfaced to the UI (we don't
    want Home turning into an inbox replacement — if you want to read
    a message, ask PILK in chat and `gmail_read` will run through the
    normal plan path).
    """
    settings = get_settings()
    creds = load_credentials(settings.google_credentials_path)
    if creds is None:
        return {"linked": False, "email": None, "unread": 0, "preview": []}
    try:
        result = await asyncio.to_thread(_inbox_glance_sync, creds)
    except Exception as e:
        log.exception("inbox_glance_failed")
        return {
            "linked": True,
            "email": creds.email,
            "unread": 0,
            "preview": [],
            "error": f"{type(e).__name__}: {e}",
        }
    return {
        "linked": True,
        "email": creds.email,
        "unread": result["unread"],
        "preview": result["preview"],
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
