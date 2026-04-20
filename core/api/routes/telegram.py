"""HTTP surface for the Telegram connect flow.

Three endpoints the Settings UI drives to make bot-setup feel like a
click-through rather than a copy-paste-hunt-through-docs exercise:

  GET  /telegram/bot-info      verify the configured bot_token + return
                               the bot's username so the UI can deep-
                               link to t.me/<username>
  POST /telegram/detect-chat   call getUpdates, find the most recent
                               chat_id that messaged the bot, and
                               return it so the UI can auto-fill the
                               chat_id field
  POST /telegram/test          send a test message to the configured
                               chat_id — proves end-to-end delivery
                               before the operator closes the card

All three resolve credentials through the same settings + integration-
secrets plumbing every other tool uses, so the operator has ONE place
to paste keys (Settings → API Keys) and the connect card reads from
that.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

from core.config import get_settings
from core.integrations.telegram import (
    TELEGRAM_API_BASE,
    TelegramClient,
    TelegramConfig,
    TelegramError,
)
from core.logging import get_logger
from core.secrets import resolve_secret

log = get_logger("pilkd.telegram.route")

router = APIRouter(prefix="/telegram")

BOT_INFO_TIMEOUT_S = 10.0


def _bot_token() -> str | None:
    s = get_settings()
    return resolve_secret("telegram_bot_token", s.telegram_bot_token)


def _chat_id() -> str | None:
    s = get_settings()
    return resolve_secret("telegram_chat_id", s.telegram_chat_id)


@router.get("/bot-info")
async def get_bot_info(_request: Request) -> dict[str, Any]:
    """Call Telegram's `getMe` with the configured token and return
    the bot's identity. Used by the connect card to (a) confirm the
    token is valid and (b) deep-link to the bot's t.me URL so the
    operator can message it in one click."""
    token = _bot_token()
    if not token:
        return {
            "configured": False,
            "error": (
                "telegram_bot_token not set. Paste the token from "
                "@BotFather in the field below, save, then retry."
            ),
        }
    try:
        async with httpx.AsyncClient(timeout=BOT_INFO_TIMEOUT_S) as c:
            r = await c.get(f"{TELEGRAM_API_BASE}/bot{token}/getMe")
    except httpx.HTTPError as e:
        log.warning("telegram_bot_info_network_error", error=str(e))
        raise HTTPException(
            status_code=502,
            detail=f"Telegram unreachable: {e}",
        ) from e
    try:
        body = r.json()
    except ValueError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Telegram returned non-JSON: {r.text[:160]!r}",
        ) from e
    if not r.is_success or not body.get("ok"):
        return {
            "configured": True,
            "valid": False,
            "error": body.get("description") or f"HTTP {r.status_code}",
        }
    me = body.get("result") or {}
    return {
        "configured": True,
        "valid": True,
        "bot_id": me.get("id"),
        "username": me.get("username"),
        "first_name": me.get("first_name"),
        "can_join_groups": me.get("can_join_groups"),
        "t_me_url": (
            f"https://t.me/{me['username']}" if me.get("username") else None
        ),
    }


@router.post("/detect-chat")
async def detect_chat(_request: Request) -> dict[str, Any]:
    """Call `getUpdates` with the configured bot_token and return the
    most recent chat_id that messaged the bot. One-click "paste your
    chat_id for me" experience — the operator just messages the bot
    `/start`, hits this endpoint, and the connect card auto-fills
    the chat_id field."""
    token = _bot_token()
    if not token:
        raise HTTPException(
            status_code=400,
            detail=(
                "telegram_bot_token not set. Save the token first; "
                "then message the bot `/start` and retry detect."
            ),
        )
    try:
        async with httpx.AsyncClient(timeout=BOT_INFO_TIMEOUT_S) as c:
            r = await c.get(f"{TELEGRAM_API_BASE}/bot{token}/getUpdates")
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Telegram unreachable: {e}",
        ) from e
    try:
        body = r.json()
    except ValueError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Telegram returned non-JSON: {r.text[:160]!r}",
        ) from e
    if not r.is_success or not body.get("ok"):
        return {
            "detected": False,
            "error": body.get("description") or f"HTTP {r.status_code}",
        }
    updates = body.get("result") or []
    # Walk updates newest-first so we pick up the operator's most
    # recent /start rather than a stale test from two days ago.
    for update in reversed(updates):
        msg = update.get("message") or update.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            return {
                "detected": True,
                "chat_id": str(chat["id"]),
                "chat_type": chat.get("type"),
                "chat_title": (
                    chat.get("title")
                    or chat.get("first_name")
                    or str(chat["id"])
                ),
            }
    return {
        "detected": False,
        "error": (
            "No messages to the bot yet. Open Telegram, find your "
            "bot, send it any message (e.g. /start), then hit "
            "Detect again."
        ),
    }


@router.post("/test")
async def send_test_message(_request: Request) -> dict[str, Any]:
    """Send a real test message using the configured credentials.
    Final step in the connect flow — if this lands on the operator's
    phone, setup is done."""
    token = _bot_token()
    chat_id = _chat_id()
    if not token or not chat_id:
        missing = [
            k for k, v in (
                ("telegram_bot_token", token),
                ("telegram_chat_id", chat_id),
            )
            if not v
        ]
        raise HTTPException(
            status_code=400,
            detail=(
                f"Not fully configured. Missing: {', '.join(missing)}."
            ),
        )
    client = TelegramClient(
        TelegramConfig(bot_token=token, chat_id=chat_id),
    )
    try:
        result = await client.send_message(
            "✅ PILK connected. You'll get push notifications here "
            "when I need an approval, finish a long-running task, "
            "or surface an incident.",
        )
    except TelegramError as e:
        return {"sent": False, "error": f"{e.status}: {e.message}"}
    return {
        "sent": True,
        "message_id": result.get("message_id"),
    }
