"""Telegram ↔ orchestrator chat bridge.

Turns the operator's Telegram chat into a first-class PILK input
surface — same free-chat semantics as the web UI, just routed via
the Bot API. The operator can send PILK a message from anywhere
(phone, laptop, in a meeting) and get back the real orchestrator
reply, not a scripted notification.

### Shape

- ``TelegramBridge`` owns one background asyncio task that long-polls
  ``getUpdates`` and dispatches inbound messages one at a time into
  the orchestrator's free-chat path.
- For each message we subscribe a one-shot hub listener, call
  ``orchestrator.run(text)``, and send the captured assistant text
  back over Telegram. The orchestrator serializes plans via its own
  lock; the bridge adds a second layer of serialization (one
  Telegram message at a time) so a burst of inbound messages doesn't
  race the orchestrator into ``OrchestratorBusyError``.
- ``getUpdates`` offset is persisted to a small JSON file under
  ``~/PILK/state/telegram-bridge.json`` so the operator doesn't
  receive stale backlog on every daemon restart.

### Defensive behavior

- If either the bot token or the chat_id is missing, the bridge
  refuses to start and logs a single ``telegram_bridge_inactive``
  line. The tool family still works for push notifications.
- Telegram API errors are swallowed — the loop sleeps and retries so
  a transient network hiccup never crashes the daemon.
- Messages from chats OTHER than the configured ``chat_id`` are
  silently ignored. Single-tenant by design; anyone else who happens
  to find the bot just talks to the void.
- Non-text messages (photos, stickers, voice) are acknowledged with
  a short "text only for now" reply rather than being silently
  dropped so the operator isn't confused.
- If the orchestrator is already running when a message arrives, we
  catch the busy error and tell the operator to try again in a
  moment. No queueing — keeps the bridge simple; the operator can
  just resend.

### What this doesn't do

- No group-chat routing. If the bot is added to a group its messages
  get filtered by chat_id like everyone else.
- No file upload support inbound. If the operator wants to hand PILK
  a document, they can drop it in ``~/PILK/workspace/`` and reference
  it by name in a text message.
- No voice transcription inbound. A short-term follow-up — the voice
  pipeline already has an STT driver we can route Telegram voice
  notes through.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from core.api.hub import Hub
from core.integrations.telegram import (
    TELEGRAM_MESSAGE_MAX_CHARS,
    TelegramClient,
    TelegramConfig,
    TelegramError,
)
from core.logging import get_logger
from core.orchestrator import Orchestrator
from core.orchestrator.orchestrator import OrchestratorBusyError

log = get_logger("pilkd.telegram.bridge")

# Telegram's long-poll maximum is 50s; we stay under that so the
# read side always finishes before the server closes the socket.
DEFAULT_LONGPOLL_S = 25
# Client-side timeout must be strictly larger than the server-side
# long-poll window; otherwise httpx cuts the socket before Telegram
# has a chance to return. +15s gives plenty of headroom.
DEFAULT_REQUEST_TIMEOUT_S = DEFAULT_LONGPOLL_S + 15
# How long to wait between retries after a Telegram API error. Keeps
# us from hammering the API on a token-revoked or 429-throttled
# failure mode.
RETRY_BACKOFF_S = 5.0
# Upper bound on how long we'll wait for a single orchestrator run
# before bailing and telling the operator. Anything past this
# strongly suggests an orchestrator-side bug or a runaway tool loop
# — we'd rather surface that than hang the bridge indefinitely.
ORCHESTRATOR_WAIT_TIMEOUT_S = 600.0


class TelegramBridge:
    """Background process that marries ``getUpdates`` to the orchestrator.

    Construct one per daemon; call ``start()`` in lifespan startup
    and ``stop()`` in lifespan shutdown.
    """

    def __init__(
        self,
        *,
        config: TelegramConfig,
        orchestrator: Orchestrator,
        hub: Hub,
        state_path: Path,
        longpoll_seconds: int = DEFAULT_LONGPOLL_S,
    ) -> None:
        self._cfg = config
        self._client = TelegramClient(
            config, timeout=DEFAULT_REQUEST_TIMEOUT_S,
        )
        self._orchestrator = orchestrator
        self._hub = hub
        self._state_path = state_path
        self._longpoll = int(longpoll_seconds)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._offset: int | None = None

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._offset = _read_offset(self._state_path)
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="telegram-bridge")
        log.info(
            "telegram_bridge_started",
            chat_id=self._cfg.chat_id,
            offset=self._offset,
        )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        # A long-poll in flight will return within ``longpoll`` seconds
        # on its own; we don't cancel aggressively because cancellation
        # mid-HTTP leaves the client socket in a weird state. Add a
        # generous wall-clock bound so shutdown can't hang forever.
        try:
            await asyncio.wait_for(task, timeout=self._longpoll + 5)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        log.info("telegram_bridge_stopped")

    # ── main loop ───────────────────────────────────────────────

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                updates = await self._client.get_updates(
                    offset=self._offset,
                    timeout=self._longpoll,
                    allowed_updates=["message"],
                    request_timeout=DEFAULT_REQUEST_TIMEOUT_S,
                )
            except TelegramError as e:
                log.warning(
                    "telegram_bridge_get_updates_failed",
                    status=e.status, message=e.message,
                )
                await self._sleep_or_stop(RETRY_BACKOFF_S)
                continue
            except Exception as e:  # network / DNS / timeout
                log.warning("telegram_bridge_poll_error", error=str(e))
                await self._sleep_or_stop(RETRY_BACKOFF_S)
                continue

            for upd in updates:
                update_id = upd.get("update_id")
                if isinstance(update_id, int):
                    self._offset = update_id + 1
                    _write_offset(self._state_path, self._offset)
                if self._stop.is_set():
                    break
                await self._handle_update(upd)

    async def _sleep_or_stop(self, seconds: float) -> None:
        # ``wait_for`` on the stop event lets shutdown interrupt
        # backoff immediately instead of forcing us to ride out the
        # full sleep.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            return

    # ── update handling ─────────────────────────────────────────

    async def _handle_update(self, upd: dict[str, Any]) -> None:
        message = upd.get("message") or {}
        chat = (message.get("chat") or {})
        from_chat_id = str(chat.get("id") or "")
        if not from_chat_id or from_chat_id != str(self._cfg.chat_id):
            # Silent drop — the bot is single-tenant; another user
            # finding it is noise, not something to chat back to.
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            # Non-text messages or empty text: one-shot reply so the
            # operator isn't left wondering why PILK ignored their
            # photo / sticker / voice note.
            with contextlib.suppress(TelegramError, Exception):
                await self._client.send_message(
                    "PILK can only read text messages right now. "
                    "Send me a message and I'll reply.",
                )
            return
        await self._dispatch(text.strip())

    async def _dispatch(self, text: str) -> None:
        """Run one message through the orchestrator and ship the reply
        back over Telegram.

        The orchestrator broadcasts ``chat.assistant`` synchronously
        (all listeners are awaited inside ``Hub.broadcast``) before
        ``run()`` returns, so we can capture the text with a one-shot
        listener, await ``run()``, and then send the captured text.
        """
        captured: dict[str, Any] = {}

        async def listener(event_type: str, payload: dict[str, Any]) -> None:
            if event_type != "chat.assistant":
                return
            # First chat.assistant event wins — there is only one per
            # run but the listener stays live after the first capture
            # to be safe.
            if "text" in captured:
                return
            captured["text"] = payload.get("text") or ""

        self._hub.subscribe(listener)
        try:
            try:
                await asyncio.wait_for(
                    self._orchestrator.run(text),
                    timeout=ORCHESTRATOR_WAIT_TIMEOUT_S,
                )
            except OrchestratorBusyError:
                await self._safe_send(
                    "PILK is working on another task right now. "
                    "Send that again in a moment and I'll pick it up.",
                )
                return
            except TimeoutError:
                await self._safe_send(
                    "That task ran past the Telegram reply window. "
                    "Check the dashboard for the final result.",
                )
                return
            except Exception as e:
                log.exception("telegram_bridge_run_failed", error=str(e))
                await self._safe_send(f"Something went wrong: {e}")
                return
        finally:
            self._hub.unsubscribe(listener)

        reply = captured.get("text") or "(no response)"
        await self._safe_send(reply)

    async def _safe_send(self, text: str) -> None:
        # The client already truncates at TELEGRAM_MESSAGE_MAX_CHARS;
        # the guard here is belt-and-braces so a pathological reply
        # never crashes the bridge.
        body = text if len(text) <= TELEGRAM_MESSAGE_MAX_CHARS else (
            text[: TELEGRAM_MESSAGE_MAX_CHARS - 16] + "\n\n… [truncated]"
        )
        try:
            await self._client.send_message(body)
        except TelegramError as e:
            log.warning(
                "telegram_bridge_send_failed",
                status=e.status, message=e.message,
            )
        except Exception as e:
            log.warning("telegram_bridge_send_error", error=str(e))


# ── offset persistence ───────────────────────────────────────────


def _read_offset(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("telegram_bridge_offset_read_failed", error=str(e))
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    offset = data.get("offset")
    return int(offset) if isinstance(offset, int) else None


def _write_offset(path: Path, offset: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"offset": int(offset)}),
            encoding="utf-8",
        )
    except OSError as e:
        log.warning("telegram_bridge_offset_write_failed", error=str(e))


__all__ = [
    "DEFAULT_LONGPOLL_S",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "ORCHESTRATOR_WAIT_TIMEOUT_S",
    "RETRY_BACKOFF_S",
    "TelegramBridge",
]
