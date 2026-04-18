"""Apple Messages surface.

  GET /integrations/apple/messages/status     availability + diagnostic
  GET /integrations/apple/messages/glance     recent threads preview

Read-only. Local only — Apple has no OAuth or API for Messages; we
read ~/Library/Messages/chat.db directly. Full Disk Access must be
granted to the Python process for this to succeed.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from core.integrations.apple import (
    check_messages_status,
    recent_threads,
)
from core.logging import get_logger

log = get_logger("pilkd.apple")

router = APIRouter(prefix="/integrations/apple")

GLANCE_PREVIEW = 4


@router.get("/messages/status")
async def messages_status() -> dict:
    status = check_messages_status()
    return status.to_public()


@router.get("/messages/glance")
async def messages_glance() -> dict:
    status = check_messages_status()
    if not status.available:
        return {
            "available": False,
            "reason": status.reason,
            "db_path": status.db_path,
            "threads": [],
        }
    try:
        threads = await asyncio.to_thread(recent_threads, GLANCE_PREVIEW)
    except Exception as e:
        log.exception("apple_glance_failed")
        return {
            "available": True,
            "threads": [],
            "error": f"{type(e).__name__}: {e}",
        }
    return {"available": True, "threads": threads}
