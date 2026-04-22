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
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.api.hub import Hub
from core.brain import Vault
from core.integrations.telegram import (
    TELEGRAM_MESSAGE_MAX_CHARS,
    TelegramClient,
    TelegramConfig,
    TelegramError,
)
from core.logging import get_logger
from core.orchestrator import Orchestrator
from core.orchestrator.orchestrator import OrchestratorBusyError

CallbackHandler = Callable[[dict[str, Any]], Awaitable[None]]

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
# When a message arrives, wait this long for follow-ups before
# dispatching. Rapid-fire "add to my previous text" messages merge
# into one turn so PILK sees them as a single intent instead of two
# disjointed conversations.
DEFAULT_COALESCE_WINDOW_S = 2.5
# Max orchestrator-busy wall-clock we'll wait before giving up on a
# queued batch. If something else is holding the lock for longer than
# this, the operator is told explicitly so they know it wasn't lost.
DEFAULT_BUSY_RETRY_BUDGET_S = 45.0
# Rolling history window. Each "turn" is two messages (user + PILK),
# so 12 turns = 24 messages — enough context for a coherent thread
# without blowing the token budget on every run.
HISTORY_MAX_TURNS = 12
# Per-turn character clamp when composing the history preamble. Keeps
# the per-message prompt bounded even if the operator sent a 50KB
# paste earlier in the thread — full text still lands in the vault.
HISTORY_TURN_CHAR_CAP = 2000
# Where auto-archived Telegram exchanges land inside the vault.
# Two sibling paths:
#   - ``chats/telegram/YYYY-MM-DD.md`` — daily digest view the operator
#     opens in Obsidian when reviewing "what did I talk about today?".
#   - ``ingested/telegram/YYYY-MM-DD-HH.md`` — per-hour file the brain
#     ingestion scanners pick up the same way they do ``ingested/
#     claude-code/`` notes, so the hydration pass surfaces recent
#     Telegram exchanges as topical context on the next turn.
CHAT_LOG_FOLDER = "chats/telegram"
INGEST_LOG_FOLDER = "ingested/telegram"


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
        callback_handler: CallbackHandler | None = None,
        vault: Vault | None = None,
        coalesce_window_s: float = DEFAULT_COALESCE_WINDOW_S,
        busy_retry_budget_s: float = DEFAULT_BUSY_RETRY_BUDGET_S,
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
        # Optional extra dispatcher for inline-button taps. The
        # approvals bridge (``core.io.telegram_approvals``) registers
        # itself here so its callback_queries ride the bridge's
        # single long-poll loop instead of racing it with a second
        # ``getUpdates`` caller.
        self._callback_handler = callback_handler
        # Optional brain vault. When set, every (user, PILK) exchange
        # is appended to the daily chat log so the operator has a
        # searchable, grep-able record without having to say
        # "remember this" every turn.
        self._vault = vault
        self._coalesce_window_s = float(coalesce_window_s)
        self._busy_retry_budget_s = float(busy_retry_budget_s)
        # Rolling conversation history, passed into every run() as a
        # preamble so PILK keeps context across turns instead of
        # re-greeting the operator mid-thread. Size is 2x turns since
        # each turn is a (user, assistant) pair.
        self._history: deque[tuple[str, str]] = deque(
            maxlen=HISTORY_MAX_TURNS * 2
        )
        # Inbound message queue drained by a single processor task.
        # Serializing here means rapid-fire messages can be coalesced
        # into one turn and we never accidentally call
        # ``orchestrator.run`` twice concurrently.
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._processor_task: asyncio.Task | None = None

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._offset = _read_offset(self._state_path)
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="telegram-bridge")
        self._processor_task = asyncio.create_task(
            self._process_queue(), name="telegram-bridge-processor"
        )
        log.info(
            "telegram_bridge_started",
            chat_id=self._cfg.chat_id,
            offset=self._offset,
        )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        processor = self._processor_task
        self._task = None
        self._processor_task = None
        if task is not None:
            # A long-poll in flight will return within ``longpoll``
            # seconds on its own; we don't cancel aggressively because
            # cancellation mid-HTTP leaves the client socket in a
            # weird state. Add a generous wall-clock bound so shutdown
            # can't hang forever.
            try:
                await asyncio.wait_for(task, timeout=self._longpoll + 5)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if processor is not None:
            processor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await processor
        log.info("telegram_bridge_stopped")

    # ── main loop ───────────────────────────────────────────────

    async def _run(self) -> None:
        # Always allow "callback_query" so approvals buttons land here
        # even when no callback handler is registered — the bridge
        # silently drops unhandled callback_queries rather than letting
        # Telegram retry them (which would cause a stuck spinner on the
        # operator's phone).
        allowed = ["message", "callback_query"]
        while not self._stop.is_set():
            try:
                updates = await self._client.get_updates(
                    offset=self._offset,
                    timeout=self._longpoll,
                    allowed_updates=allowed,
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
        # Inline-button taps arrive as callback_query updates, not
        # plain messages. Hand them off to the approvals bridge and
        # return — we deliberately do NOT fall through to the chat
        # path (a callback_query has no ``message.text`` anyway, but
        # the explicit branch keeps the code obvious to the next
        # reader).
        if upd.get("callback_query") is not None:
            if self._callback_handler is not None:
                try:
                    await self._callback_handler(upd)
                except Exception as e:
                    log.warning(
                        "telegram_bridge_callback_handler_failed",
                        error=str(e),
                    )
            return
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
        # Queue for the processor; it handles coalescing + serialization.
        await self._queue.put(text.strip())

    async def _process_queue(self) -> None:
        """Drain inbound messages one coalesced batch at a time.

        A single processor task owns all orchestrator dispatches so
        two messages can never race each other into ``run()``. After
        pulling the first message off the queue, we wait up to
        ``coalesce_window_s`` for follow-ups and merge them into one
        turn — the "I want to add to that" flow the operator keeps
        hitting.
        """
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                first = await self._queue.get()
            except asyncio.CancelledError:
                return

            # Coalesce any follow-ups that arrive within the window.
            parts = [first]
            deadline = loop.time() + self._coalesce_window_s
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    more = await asyncio.wait_for(
                        self._queue.get(), timeout=remaining
                    )
                except TimeoutError:
                    break
                parts.append(more)

            merged = "\n\n".join(p for p in parts if p)
            if not merged:
                continue

            try:
                await self._dispatch(merged)
            except Exception as e:  # pragma: no cover - defense-in-depth
                log.exception(
                    "telegram_bridge_processor_error", error=str(e)
                )

    async def _dispatch(self, text: str) -> None:
        """Run one (possibly coalesced) message through the orchestrator
        and ship the reply back over Telegram.

        Prepends a rolling conversation-history preamble to ``text`` so
        PILK stays in-context across turns instead of re-greeting
        mid-thread. Waits a bounded amount of time when the
        orchestrator is busy rather than dropping the message.
        """
        captured: dict[str, Any] = {}

        async def listener(event_type: str, payload: dict[str, Any]) -> None:
            if event_type != "chat.assistant":
                return
            if "text" in captured:
                return
            captured["text"] = payload.get("text") or ""

        prompt = _compose_prompt(list(self._history), text)

        self._hub.subscribe(listener)
        try:
            ran = await self._run_with_busy_retry(prompt)
            if ran is _BusyExhausted:
                await self._safe_send(
                    "PILK is still finishing something else — try "
                    "again in a minute and I'll pick it up.",
                )
                return
            if ran is _Timeout:
                await self._safe_send(
                    "That task ran past the Telegram reply window. "
                    "Check the dashboard for the final result.",
                )
                return
            if isinstance(ran, BaseException):
                log.exception("telegram_bridge_run_failed", error=str(ran))
                await self._safe_send(f"Something went wrong: {ran}")
                return
        finally:
            self._hub.unsubscribe(listener)

        reply = captured.get("text") or "(no response)"
        await self._safe_send(reply)

        # Only record the exchange once PILK actually replied — a
        # busy-exhausted or timed-out dispatch shouldn't pollute
        # history with a half-turn the model never saw.
        self._history.append(("user", text))
        self._history.append(("assistant", reply))
        await self._persist_exchange(text, reply)

    async def _run_with_busy_retry(self, prompt: str) -> Any:
        """Call ``orchestrator.run`` with a bounded busy-retry loop.

        Returns ``None`` on success, the sentinel ``_BusyExhausted``
        when the retry budget is spent, ``_Timeout`` on
        ``TimeoutError``, or the raised exception on any other failure
        — the caller decides how to surface each to the operator.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._busy_retry_budget_s
        backoff = 0.75
        while True:
            try:
                await asyncio.wait_for(
                    self._orchestrator.run(prompt),
                    timeout=ORCHESTRATOR_WAIT_TIMEOUT_S,
                )
                return None
            except OrchestratorBusyError:
                if loop.time() >= deadline:
                    return _BusyExhausted
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 4.0)
            except TimeoutError:
                return _Timeout
            except Exception as e:
                return e

    async def _persist_exchange(
        self, user_text: str, assistant_text: str
    ) -> None:
        """Persist one exchange to the vault in two shapes.

        * ``chats/telegram/YYYY-MM-DD.md`` — daily digest the
          operator browses in Obsidian.
        * ``ingested/telegram/YYYY-MM-DD-HH.md`` — per-hour file
          the memory hydration + brain search layers pick up as
          recent context on subsequent turns.

        Silent-fail by design — a vault write error must never crash
        the bridge or block the reply the operator is waiting on.
        """
        if self._vault is None:
            return
        now = datetime.now(UTC)
        block = (
            f"## {now.strftime('%H:%M UTC')}\n\n"
            f"**Me:** {user_text}\n\n"
            f"**PILK:** {assistant_text}\n"
        )
        day_rel = f"{CHAT_LOG_FOLDER}/{now.strftime('%Y-%m-%d')}.md"
        hour_rel = (
            f"{INGEST_LOG_FOLDER}/{now.strftime('%Y-%m-%d-%H')}.md"
        )
        try:
            await self._append_or_create(
                day_rel,
                block,
                header=f"# Telegram — {now.strftime('%Y-%m-%d')}\n\n",
            )
        except Exception as e:
            log.warning("telegram_bridge_chatlog_failed", error=str(e))
        try:
            await self._append_or_create(
                hour_rel,
                block,
                header=(
                    f"# Telegram — {now.strftime('%Y-%m-%d %H:00 UTC')}\n\n"
                ),
            )
        except Exception as e:
            log.warning("telegram_bridge_ingestlog_failed", error=str(e))

    async def _append_or_create(
        self, rel: str, block: str, *, header: str,
    ) -> None:
        """Idempotently append ``block`` to ``rel`` in the vault.

        Reads first to learn if the file exists; the vault's ``read``
        raises FileNotFoundError when the file is missing, which we
        turn into a fresh write with the given ``header`` as a
        preamble. Subsequent writes append to the same file.
        """
        assert self._vault is not None
        exists = True
        try:
            self._vault.read(rel)
        except FileNotFoundError:
            exists = False
        except Exception:
            # Unknown read error — skip the idempotency probe and
            # force a write. Worst case the header duplicates, which
            # is harmless.
            exists = False
        if exists:
            await asyncio.to_thread(
                self._vault.write, rel, block, append=True,
            )
        else:
            await asyncio.to_thread(
                self._vault.write, rel, header + block,
            )

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


# ── sentinels used by ``_run_with_busy_retry`` ────────────────────


class _BusyExhausted:
    """Marker: the orchestrator stayed busy past the retry budget."""


class _Timeout:
    """Marker: the run blew through ``ORCHESTRATOR_WAIT_TIMEOUT_S``."""


# ── prompt composition ────────────────────────────────────────────


def _compose_prompt(
    history: list[tuple[str, str]], current: str
) -> str:
    """Format ``history`` + ``current`` into a single prompt.

    PILK's free-chat path doesn't take a history parameter, so we
    stuff it into the user message itself. The format is plain enough
    that the model reliably treats the preamble as context rather
    than as instructions from the operator.
    """
    if not history:
        return current
    lines = ["[Conversation so far — rolling window]"]
    for role, text in history:
        label = "Me" if role == "user" else "PILK"
        body = text or ""
        if len(body) > HISTORY_TURN_CHAR_CAP:
            body = body[:HISTORY_TURN_CHAR_CAP] + "… [truncated]"
        lines.append(f"{label}: {body}")
    lines.append("")
    lines.append("[New message]")
    lines.append(current)
    return "\n".join(lines)


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
    "CHAT_LOG_FOLDER",
    "DEFAULT_BUSY_RETRY_BUDGET_S",
    "DEFAULT_COALESCE_WINDOW_S",
    "DEFAULT_LONGPOLL_S",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "HISTORY_MAX_TURNS",
    "HISTORY_TURN_CHAR_CAP",
    "INGEST_LOG_FOLDER",
    "ORCHESTRATOR_WAIT_TIMEOUT_S",
    "RETRY_BACKOFF_S",
    "TelegramBridge",
]
