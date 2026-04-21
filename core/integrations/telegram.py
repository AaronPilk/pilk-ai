"""Telegram Bot API client — thin httpx wrapper for push notifications.

Lets PILK (or any agent) push a message / document to the operator's
chat without waiting for the operator to initiate the conversation.
Two endpoints covered:

    sendMessage    plain or Markdown/HTML text
    sendDocument   attach a file (PDF report, CSV shortlist, etc.)

Single-tenant by design for V1: one bot token + one chat_id = one
operator. The chat_id is stored in settings so every agent that wants
to push to the operator resolves to the same destination. Per-user
bots + a chat_id-per-role table can land in Phase 2 without touching
the client.

Docs: https://core.telegram.org/bots/api
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from core.logging import get_logger

log = get_logger("pilkd.telegram")

TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT_S = 15.0

# Telegram hard-caps a single message at 4096 chars; sendMessage will
# 400 above that. We truncate with a trailing ellipsis rather than
# split so the operator sees the important opening + knows there was
# more. Agents that need to send volume should use sendDocument with
# the long body in a file instead.
TELEGRAM_MESSAGE_MAX_CHARS = 4096


class TelegramError(Exception):
    def __init__(self, status: int, message: str, raw: Any = None):
        super().__init__(f"Telegram {status}: {message}")
        self.status = status
        self.message = message
        self.raw = raw


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str  # str, not int — Telegram accepts either but strings
                  # survive YAML / env round-trips cleanly.
    api_base: str = TELEGRAM_API_BASE


class TelegramClient:
    def __init__(
        self, config: TelegramConfig, *, timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._cfg = config
        self._timeout = timeout

    def _url(self, method: str) -> str:
        return f"{self._cfg.api_base}/bot{self._cfg.bot_token}/{method}"

    async def send_message(
        self,
        text: str,
        *,
        chat_id: str | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        """Send a plain text / markdown message. Long messages get
        truncated — callers that need to deliver a long document
        should use send_document with a file instead."""
        body_text = text or ""
        if len(body_text) > TELEGRAM_MESSAGE_MAX_CHARS:
            # Compute the exact prefix length from the suffix so the
            # result always fits under the 4096 cap — counting
            # characters by hand drifts as the suffix copy changes.
            suffix = (
                "\n\n… [truncated — use sendDocument for long content]"
            )
            prefix_len = TELEGRAM_MESSAGE_MAX_CHARS - len(suffix)
            body_text = body_text[:prefix_len] + suffix
        payload: dict[str, Any] = {
            "chat_id": chat_id or self._cfg.chat_id,
            "text": body_text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            # Telegram supports MarkdownV2 + HTML. MarkdownV2 requires
            # escaping special characters; leave that to the caller.
            payload["parse_mode"] = parse_mode
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(self._url("sendMessage"), json=payload)
        return _decode(r, "sendMessage")

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 25,
        allowed_updates: list[str] | None = None,
        request_timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """Long-poll Telegram for new updates.

        ``timeout`` is the server-side long-poll window in seconds —
        Telegram holds the connection open that long waiting for new
        activity and only then returns. ``request_timeout`` is the
        client-side HTTP timeout; it must be strictly larger than
        ``timeout`` or httpx cuts the call off before Telegram is
        ready to answer.

        Returns the raw list of update objects. The caller is
        responsible for advancing ``offset`` past the last update_id
        seen so Telegram doesn't redeliver them.
        """
        params: dict[str, Any] = {"timeout": int(timeout)}
        if offset is not None:
            params["offset"] = int(offset)
        if allowed_updates is not None:
            # Telegram expects this as a JSON-encoded string, not an
            # array in form-data. httpx json-encodes when we pass the
            # whole dict as `json=`, so we keep it as a list.
            params["allowed_updates"] = allowed_updates
        http_timeout = request_timeout if request_timeout is not None else (
            float(timeout) + 10.0
        )
        async with httpx.AsyncClient(timeout=http_timeout) as c:
            r = await c.post(self._url("getUpdates"), json=params)
        result = _decode(r, "getUpdates")
        # ``getUpdates`` returns an array in ``result``; ``_decode``
        # hands back whatever ``result`` is (list here rather than
        # dict). Normalize defensively.
        if isinstance(result, list):
            return result
        return []

    async def send_document(
        self,
        path: Path,
        *,
        caption: str | None = None,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        """Attach a file. Captions are capped at 1024 chars by
        Telegram; we enforce that here so the API doesn't 400."""
        p = Path(path)
        if not p.is_file():
            raise TelegramError(
                status=400, message=f"file not found: {p}",
            )
        data: dict[str, Any] = {
            "chat_id": chat_id or self._cfg.chat_id,
        }
        if caption:
            data["caption"] = caption[:1024]
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            with p.open("rb") as fh:
                files = {
                    "document": (p.name, fh, "application/octet-stream"),
                }
                r = await c.post(
                    self._url("sendDocument"), data=data, files=files,
                )
        return _decode(r, "sendDocument")


def _decode(resp: httpx.Response, method: str) -> dict[str, Any]:
    """Telegram always returns a 200 with ``{ok: false, description:…}``
    on API errors; we hoist that into a TelegramError so callers treat
    transport + API errors the same way."""
    try:
        body = resp.json()
    except ValueError:
        raise TelegramError(
            status=resp.status_code,
            message=f"{method}: non-JSON response ({resp.text[:160]!r})",
        ) from None
    if not resp.is_success:
        desc = (body or {}).get("description") or f"HTTP {resp.status_code}"
        raise TelegramError(
            status=resp.status_code, message=desc, raw=body,
        )
    if not body.get("ok"):
        raise TelegramError(
            status=resp.status_code,
            message=body.get("description") or "Telegram API returned ok=false",
            raw=body,
        )
    return body.get("result") or {}


__all__ = [
    "TELEGRAM_API_BASE",
    "TELEGRAM_MESSAGE_MAX_CHARS",
    "TelegramClient",
    "TelegramConfig",
    "TelegramError",
]
