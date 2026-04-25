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

### Inbound attachments

Telegram messages can carry a photo, a document, or a voice note.
The bridge resolves them via the Bot API's ``getFile`` endpoint,
downloads the bytes, and saves them to the same ``AttachmentStore``
the web chat uses. The orchestrator then sees them as ``ChatAttachment``
records on the next ``run()`` call — exactly the same path the web
upload flow uses for vision / document / text content blocks. So
"send PILK a screenshot of this dashboard" works.

Voice notes are transcribed via OpenAI Whisper (when an
``OPENAI_API_KEY`` is configured) and the transcript becomes the
turn's text — no audio attachment is created. Without a Whisper
key, voice messages bounce back with a "configure OpenAI key for
voice transcription" hint.

Caption support: if the operator attaches a photo/document with
text in the caption, the caption becomes the user message and the
file becomes the attachment. Without a caption we fall back to a
default question ("describe what you see / what is this") so PILK
has *something* to act on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.api.hub import Hub
from core.brain import Vault
from core.chat.attachments import (
    AttachmentError,
    AttachmentStore,
    is_allowed_mime,
)
from core.integrations.telegram import (
    TELEGRAM_MESSAGE_MAX_CHARS,
    TelegramClient,
    TelegramConfig,
    TelegramError,
)
from core.logging import get_logger
from core.orchestrator import Orchestrator
from core.orchestrator.orchestrator import ChatAttachment, OrchestratorBusyError

CallbackHandler = Callable[[dict[str, Any]], Awaitable[None]]

# Per-call cap on inbound Telegram file size. Telegram's getFile
# tops out around 20 MiB anyway; we cap matching that and the
# AttachmentStore's own MAX_ATTACHMENT_BYTES.
MAX_TELEGRAM_FILE_BYTES = 20 * 1024 * 1024

# Plain-English fallback prompts when the operator sends a photo
# or document without a caption — gives PILK something to act on.
DEFAULT_PHOTO_PROMPT = (
    "Look at this photo and tell me what you see. If there's "
    "anything actionable for me, call it out."
)
DEFAULT_DOCUMENT_PROMPT = (
    "Read this document and summarize it. Flag anything that "
    "looks important or like it needs a follow-up."
)
DEFAULT_VOICE_TRANSCRIPT_PROMPT = (
    "(Transcribed voice note follows.)"
)

# Telegram document MIMEs we map onto our attachment store. Anything
# else gets a friendly "I can't read that file type yet" reply.
_TELEGRAM_VOICE_MIME = "audio/ogg"
_TELEGRAM_PHOTO_MIME = "image/jpeg"  # Telegram delivers photos as JPEG


@dataclass
class _InboundMessage:
    """One coalesce-able message from Telegram. Carries the operator
    text plus any attachment IDs that have already been saved into
    the chat AttachmentStore.

    The bridge passes a list of these through ``_supervise_dispatch``
    instead of plain strings so a coalesced batch can carry both
    text and files (e.g. operator types a setup line, then drops a
    screenshot a half-second later — both should land in the same
    orchestrator turn)."""

    text: str
    attachment_ids: list[str] = field(default_factory=list)


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
# How long we wait for an in-flight dispatch to wind down after we
# issue an interrupt-and-merge cancel. Cancellation is cooperative —
# the orchestrator checks the flag between turns — so the dispatch
# typically exits within a turn's worth of work. 15s covers an
# already-in-flight tool call that has to finish + one more turn to
# observe the flag; beyond that we give up waiting and move on with
# the merged batch anyway.
DEFAULT_CANCEL_WAIT_S = 15.0
# Sentinel cancel-reason string the bridge uses when the operator
# double-messages. The driver surfaces the reason both to the
# orchestrator (as the cancel reason on the broadcast) and back to
# the bridge on the `chat.assistant` "Task cancelled: …" message — we
# use it to tell "the operator corrected course" apart from any
# other cancellation path so we don't ship the cancellation text to
# Telegram and confuse them.
CANCEL_REASON_FOLLOW_UP = "operator follow-up — merging"
# Shortest pause before we start coalescing follow-ups ON TOP of an
# already in-flight dispatch. We want "send → 200ms later send again"
# to arrive at PILK as one thought, so we briefly drain the queue
# after the first interrupting message lands before issuing the
# cancel. Smaller than the initial coalesce window since by this
# point the operator is typing a correction, not composing from scratch.
FOLLOW_UP_COALESCE_WINDOW_S = 1.0
# Rolling history window. Each "turn" is two messages (user + PILK),
# so 12 turns = 24 messages — enough context for a coherent thread
# without blowing the token budget on every run.
HISTORY_MAX_TURNS = 12
# Per-turn character clamp when composing the history preamble. Keeps
# the per-message prompt bounded even if the operator sent a 50KB
# paste earlier in the thread — full text still lands in the vault.
HISTORY_TURN_CHAR_CAP = 2000
# Where auto-archived Telegram exchanges land inside the vault.
# Three sibling paths:
#   - ``chats/telegram/YYYY-MM-DD.md`` — daily digest view the operator
#     opens in Obsidian when reviewing "what did I talk about today?".
#   - ``ingested/telegram/YYYY-MM-DD-HH.md`` — per-hour file the brain
#     ingestion scanners pick up the same way they do ``ingested/
#     claude-code/`` notes, so the hydration pass surfaces recent
#     Telegram exchanges as topical context on the next turn.
#   - ``sessions/telegram/{session_id}.md`` — one file per conversation
#     session (see ``_ChatSession``). Surfaces in the Brain → Sessions
#     category as a complete readable thread instead of being sliced
#     into arbitrary per-hour files.
CHAT_LOG_FOLDER = "chats/telegram"
INGEST_LOG_FOLDER = "ingested/telegram"
SESSION_LOG_FOLDER = "sessions/telegram"

# Idle gap between consecutive inbound messages that closes the
# current session and opens a new one. Default 15 minutes: short
# enough that a genuine "new topic after lunch" message starts fresh,
# long enough that a brief thinking pause mid-thread doesn't shatter
# a real conversation into fragments. Overridable for testing via the
# ``PILK_TELEGRAM_SESSION_IDLE_GAP_S`` env var.
DEFAULT_SESSION_IDLE_GAP_S = 15 * 60.0


class _ChatSession:
    """Idle-timeout-driven session boundary tracker.

    A "session" is a contiguous run of inbound user messages where
    each consecutive pair arrives within ``idle_gap_s`` seconds of
    each other. Anything longer closes the old session and opens a
    fresh one with a new ``session_id``.

    Why this exists: prior behavior treated every inbound message as
    an independent orchestrator plan, so a one-hour Telegram
    conversation showed up as ~40 disjointed "sessions" in the
    dashboard and memory store. Grouping them by idle gap gives the
    memory hydration + Brain UI something the operator actually
    recognizes as a conversation.

    State is small enough to round-trip through the bridge's JSON
    state file on every tick, so an accidental pilkd restart mid-
    session doesn't drop the operator into a brand-new session.
    """

    def __init__(self, idle_gap_s: float) -> None:
        self._idle_gap_s = float(idle_gap_s)
        self._session_id: str | None = None
        self._last_activity: datetime | None = None
        self._started_at: datetime | None = None
        self._message_count = 0

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    def tick(self, now: datetime) -> tuple[str, bool]:
        """Advance the tracker by one inbound message at ``now``.

        Returns ``(session_id, is_new_session)``. ``is_new_session``
        is True on the first message ever and after any idle gap
        larger than ``idle_gap_s``.
        """
        is_new = (
            self._session_id is None
            or self._last_activity is None
            or (now - self._last_activity).total_seconds() > self._idle_gap_s
        )
        if is_new:
            # Format is deliberately human-readable and naturally
            # sortable — the session_id doubles as the vault filename
            # under ``sessions/telegram/`` so Obsidian's sidebar lists
            # them chronologically without extra metadata.
            self._session_id = now.strftime("tg-%Y%m%d-%H%M%S")
            self._started_at = now
            self._message_count = 0
        self._last_activity = now
        self._message_count += 1
        assert self._session_id is not None
        return self._session_id, is_new

    def as_state(self) -> dict[str, Any]:
        return {
            "id": self._session_id,
            "started_at": (
                self._started_at.isoformat() if self._started_at else None
            ),
            "last_activity": (
                self._last_activity.isoformat() if self._last_activity else None
            ),
            "message_count": self._message_count,
        }

    def load_state(self, data: dict[str, Any]) -> None:
        sid = data.get("id")
        if isinstance(sid, str):
            self._session_id = sid
        started = data.get("started_at")
        if isinstance(started, str):
            try:
                self._started_at = datetime.fromisoformat(started)
            except ValueError:
                self._started_at = None
        last = data.get("last_activity")
        if isinstance(last, str):
            try:
                self._last_activity = datetime.fromisoformat(last)
            except ValueError:
                self._last_activity = None
        mc = data.get("message_count")
        if isinstance(mc, int):
            self._message_count = mc


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
        session_idle_gap_s: float | None = None,
        attachment_store: AttachmentStore | None = None,
        openai_api_key: str | None = None,
    ) -> None:
        self._cfg = config
        self._client = TelegramClient(
            config, timeout=DEFAULT_REQUEST_TIMEOUT_S,
        )
        self._orchestrator = orchestrator
        self._hub = hub
        # Where downloaded photos/documents land. Without a store the
        # bridge degrades to text-only and tells the operator instead
        # of silently dropping files.
        self._attachment_store = attachment_store
        # OpenAI key for Whisper transcription of voice notes. Without
        # it, voice messages get a polite "configure OPENAI_API_KEY"
        # reply rather than vanishing.
        self._openai_api_key = openai_api_key
        self._state_path = state_path
        self._longpoll = int(longpoll_seconds)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._offset: int | None = None
        # Session boundary tracker. Env override lets tests shrink the
        # idle gap without a code change.
        env_gap = os.environ.get("PILK_TELEGRAM_SESSION_IDLE_GAP_S")
        gap = session_idle_gap_s
        if gap is None:
            try:
                gap = float(env_gap) if env_gap else DEFAULT_SESSION_IDLE_GAP_S
            except ValueError:
                gap = DEFAULT_SESSION_IDLE_GAP_S
        self._session = _ChatSession(idle_gap_s=gap)
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
        self._queue: asyncio.Queue[_InboundMessage] = asyncio.Queue()
        # Set by ``_supervise_dispatch`` when it cancels an in-flight
        # dispatch to fold in follow-up messages. The currently-running
        # ``_dispatch`` reads this flag after its orchestrator call
        # returns and, when true, skips the "send reply + record
        # history + persist exchange" tail so the operator only sees
        # the final merged reply rather than a confusing cancellation
        # notice for their own correction.
        self._current_dispatch_cancelled_for_follow_up: bool = False
        self._processor_task: asyncio.Task | None = None

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._load_state()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="telegram-bridge")
        self._processor_task = asyncio.create_task(
            self._process_queue(), name="telegram-bridge-processor"
        )
        log.info(
            "telegram_bridge_started",
            chat_id=self._cfg.chat_id,
            offset=self._offset,
            session_id=self._session.session_id,
            history_turns=len(self._history) // 2,
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
                    self._save_state()
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

        # Try, in order:
        #   1. plain text — fast path, original behaviour
        #   2. photo — Telegram sends an array of size variants, we
        #      pick the largest and hand it off as an image attachment
        #   3. document — sent as a file (image/pdf), goes through the
        #      same store as long as the mime is allowed
        #   4. voice / audio — transcribe via Whisper, treat as text
        # Anything else (sticker, location, contact, video without
        # support) gets a polite reply explaining what we can read.
        text = message.get("text")
        caption = message.get("caption") or ""
        photos = message.get("photo")
        document = message.get("document")
        voice = message.get("voice") or message.get("audio")
        video = message.get("video")

        try:
            if isinstance(text, str) and text.strip():
                await self._queue.put(
                    _InboundMessage(text=text.strip())
                )
                return

            if isinstance(photos, list) and photos:
                inbound = await self._ingest_photo(photos, caption)
                if inbound is not None:
                    await self._queue.put(inbound)
                return

            if isinstance(document, dict):
                inbound = await self._ingest_document(document, caption)
                if inbound is not None:
                    await self._queue.put(inbound)
                return

            if isinstance(voice, dict):
                inbound = await self._ingest_voice(voice, caption)
                if inbound is not None:
                    await self._queue.put(inbound)
                return

            if isinstance(video, dict):
                # Videos can be huge and Claude vision wants frames,
                # not raw video. Keep this honest: tell the operator
                # to use ``analyze_video_url`` instead.
                with contextlib.suppress(TelegramError, Exception):
                    await self._client.send_message(
                        "Video file uploads aren't supported yet — "
                        "but if you paste a video LINK (Instagram / "
                        "TikTok / YouTube / Twitter) I'll watch and "
                        "summarise it.",
                    )
                return

            # Sticker, location, contact, etc.
            with contextlib.suppress(TelegramError, Exception):
                await self._client.send_message(
                    "I can read text, photos, documents (PDFs / "
                    "images), and voice notes. Anything else hasn't "
                    "got a hand-off yet.",
                )
        except Exception as e:  # pragma: no cover - defense in depth
            log.exception(
                "telegram_bridge_inbound_handle_failed", error=str(e),
            )
            with contextlib.suppress(TelegramError, Exception):
                await self._client.send_message(
                    "Something tripped while I was reading that — "
                    "give me a sec and try again.",
                )

    async def _ingest_photo(
        self, photos: list[Any], caption: str,
    ) -> _InboundMessage | None:
        """Telegram sends ``photo`` as a list of progressively larger
        size variants. Pick the largest; that's the one Claude vision
        gets the most signal from."""
        if self._attachment_store is None:
            with contextlib.suppress(TelegramError, Exception):
                await self._client.send_message(
                    "Photos aren't wired up on this PILK install — "
                    "ask the operator to enable the chat attachment "
                    "store.",
                )
            return None
        sized = [
            p for p in photos
            if isinstance(p, dict) and p.get("file_id")
        ]
        if not sized:
            return None
        # Pick by file_size when available, otherwise by w*h.
        def _area(p: dict) -> int:
            if "file_size" in p:
                with contextlib.suppress(TypeError, ValueError):
                    return int(p["file_size"])
            try:
                return int(p.get("width", 0)) * int(p.get("height", 0))
            except (TypeError, ValueError):
                return 0

        biggest = max(sized, key=_area)
        att_id = await self._download_and_save(
            file_id=str(biggest["file_id"]),
            mime=_TELEGRAM_PHOTO_MIME,
            filename="telegram-photo.jpg",
        )
        if att_id is None:
            return None
        prompt = (caption or "").strip() or DEFAULT_PHOTO_PROMPT
        return _InboundMessage(text=prompt, attachment_ids=[att_id])

    async def _ingest_document(
        self, document: dict[str, Any], caption: str,
    ) -> _InboundMessage | None:
        if self._attachment_store is None:
            with contextlib.suppress(TelegramError, Exception):
                await self._client.send_message(
                    "Documents aren't wired up on this PILK install.",
                )
            return None
        file_id = str(document.get("file_id") or "")
        if not file_id:
            return None
        mime = (
            str(document.get("mime_type") or "")
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        filename = (
            str(document.get("file_name") or "telegram-document").strip()
        )
        if not is_allowed_mime(mime):
            with contextlib.suppress(TelegramError, Exception):
                await self._client.send_message(
                    f"That file type ({mime or 'unknown'}) isn't "
                    "supported yet. I can read images (PNG / JPEG / "
                    "GIF / WebP), PDFs, and plain text / markdown / "
                    "CSV / JSON.",
                )
            return None
        att_id = await self._download_and_save(
            file_id=file_id, mime=mime, filename=filename,
        )
        if att_id is None:
            return None
        prompt = (caption or "").strip() or DEFAULT_DOCUMENT_PROMPT
        return _InboundMessage(text=prompt, attachment_ids=[att_id])

    async def _ingest_voice(
        self, voice: dict[str, Any], caption: str,
    ) -> _InboundMessage | None:
        if not self._openai_api_key:
            with contextlib.suppress(TelegramError, Exception):
                await self._client.send_message(
                    "Voice notes need an OpenAI key configured for "
                    "transcription — once that's set I can read "
                    "voice messages. Type for now.",
                )
            return None
        file_id = str(voice.get("file_id") or "")
        if not file_id:
            return None
        try:
            meta = await self._client.get_file(file_id)
            file_path = str(meta.get("file_path") or "")
            if not file_path:
                raise TelegramError(
                    status=500, message="getFile returned no file_path",
                )
            audio_bytes = await self._client.download_file(file_path)
        except (TelegramError, Exception) as e:
            log.warning(
                "telegram_voice_download_failed", error=str(e),
            )
            with contextlib.suppress(TelegramError, Exception):
                await self._client.send_message(
                    "Couldn't fetch that voice note. Try again or "
                    "type the message instead.",
                )
            return None
        try:
            transcript = await self._transcribe_voice(audio_bytes)
        except Exception as e:
            log.warning(
                "telegram_voice_transcribe_failed", error=str(e),
            )
            with contextlib.suppress(TelegramError, Exception):
                await self._client.send_message(
                    "Whisper didn't accept that voice clip. Type the "
                    "message instead?",
                )
            return None
        if not transcript.strip():
            return None
        prefix = (caption.strip() + "\n\n") if caption.strip() else ""
        full_text = f"{prefix}{DEFAULT_VOICE_TRANSCRIPT_PROMPT}\n{transcript.strip()}"
        return _InboundMessage(text=full_text)

    async def _transcribe_voice(self, audio_bytes: bytes) -> str:
        """One-shot Whisper call. Local helper rather than reusing
        the voice OpenAISTT driver because we don't want a structured
        Transcript record here — just the text."""
        # Local import keeps httpx out of the module-load path for
        # the bridge until it's actually used.
        import httpx

        async with httpx.AsyncClient(timeout=120.0) as c:
            files = {
                "file": (
                    "telegram-voice.ogg",
                    audio_bytes,
                    _TELEGRAM_VOICE_MIME,
                ),
                "model": (None, "whisper-1"),
                "response_format": (None, "text"),
            }
            r = await c.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={
                    "Authorization": f"Bearer {self._openai_api_key}",
                },
                files=files,
            )
            if r.status_code >= 400:
                raise RuntimeError(
                    f"whisper {r.status_code}: {r.text[:300]}"
                )
            return r.text.strip()

    async def _download_and_save(
        self, *, file_id: str, mime: str, filename: str,
    ) -> str | None:
        """Common photo/document download path. Returns the saved
        attachment id, or None on failure (with the operator already
        notified)."""
        assert self._attachment_store is not None  # caller guards
        try:
            meta = await self._client.get_file(file_id)
        except (TelegramError, Exception) as e:
            log.warning(
                "telegram_get_file_failed", error=str(e),
            )
            with contextlib.suppress(TelegramError, Exception):
                await self._client.send_message(
                    "Couldn't grab that file from Telegram — try "
                    "sending it again?",
                )
            return None
        file_path = str(meta.get("file_path") or "")
        size = meta.get("file_size")
        if not file_path:
            log.warning("telegram_get_file_no_path", file_id=file_id[:20])
            return None
        if isinstance(size, int) and size > MAX_TELEGRAM_FILE_BYTES:
            with contextlib.suppress(TelegramError, Exception):
                await self._client.send_message(
                    f"That file is {size // (1024 * 1024)} MB which "
                    "is bigger than I can read in chat (cap is 20 "
                    "MB). Drop it in ~/PILK/workspace/ instead and "
                    "tell me the filename.",
                )
            return None
        try:
            payload = await self._client.download_file(file_path)
        except (TelegramError, Exception) as e:
            log.warning(
                "telegram_download_failed", error=str(e),
            )
            with contextlib.suppress(TelegramError, Exception):
                await self._client.send_message(
                    "Couldn't download that file — try once more?",
                )
            return None
        try:
            attachment = self._attachment_store.save(
                payload=payload, mime=mime, filename=filename,
            )
        except AttachmentError as e:
            with contextlib.suppress(TelegramError, Exception):
                await self._client.send_message(
                    f"Couldn't store that file: {e}",
                )
            return None
        log.info(
            "telegram_attachment_saved",
            attachment_id=attachment.id,
            kind=attachment.kind,
            mime=attachment.mime,
            size=attachment.size,
        )
        return attachment.id

    async def _process_queue(self) -> None:
        """Drain inbound messages with interrupt-and-merge semantics.

        Two jobs:

        1. **Initial coalesce.** After the first message lands, wait up
           to ``coalesce_window_s`` for fast follow-ups and merge them
           into one prompt — the "add to my previous text" pattern.
        2. **Live interrupt.** Once dispatch is in flight, keep
           watching the queue. If a new message arrives mid-run we
           cancel the in-flight plan, drain any near-simultaneous
           follow-ups, merge everything into one prompt, and
           redispatch. PILK sees the full intent as a single turn
           instead of finishing an outdated plan and then starting
           over from a stripped follow-up message.

        This is how we honor "I can double-message and PILK will stop,
        read both, and continue on the updated path" without races or
        dropped context.
        """
        while not self._stop.is_set():
            try:
                first = await self._queue.get()
            except asyncio.CancelledError:
                return
            parts = [first]
            parts.extend(await self._coalesce_from_queue(self._coalesce_window_s))
            try:
                await self._supervise_dispatch(parts)
            except Exception as e:  # pragma: no cover - defense-in-depth
                log.exception(
                    "telegram_bridge_processor_error", error=str(e)
                )

    async def _coalesce_from_queue(
        self, window_s: float,
    ) -> list[_InboundMessage]:
        """Drain any messages that arrive within ``window_s`` of now.

        Shared between the initial coalesce (after the first message
        of a batch) and the follow-up coalesce (after the operator
        interrupts an in-flight dispatch). Separated so the two flows
        can pick their own window — long enough to catch a typo fix,
        short enough not to add perceptible lag.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, window_s)
        extras: list[_InboundMessage] = []
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
            extras.append(more)
        return extras

    async def _supervise_dispatch(
        self, parts: list[_InboundMessage],
    ) -> None:
        """Dispatch the batch, watching the queue for interrupting messages.

        Runs ``_dispatch`` in a child task while ``_queue.get()`` races
        it. If a new message wins that race, we cancel PILK's in-flight
        plan with a sentinel reason, drain any immediate follow-ups,
        merge them on top of the current batch, and loop to redispatch
        the combined intent.
        """
        while True:
            merged_text = "\n\n".join(p.text for p in parts if p.text)
            merged_attachment_ids = [
                aid
                for p in parts
                for aid in p.attachment_ids
            ]
            if not merged_text and not merged_attachment_ids:
                return
            self._current_dispatch_cancelled_for_follow_up = False
            dispatch_task = asyncio.create_task(
                self._dispatch(merged_text, merged_attachment_ids),
                name="telegram-bridge-dispatch",
            )
            peek_task = asyncio.create_task(
                self._queue.get(), name="telegram-bridge-peek",
            )
            done, _pending = await asyncio.wait(
                {dispatch_task, peek_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if dispatch_task in done:
                # Normal completion. Cancel the peek watcher; if a
                # message happened to land in the exact same tick,
                # feed it back into the queue so the next outer loop
                # iteration picks it up as a fresh batch.
                peek_task.cancel()
                with contextlib.suppress(
                    asyncio.CancelledError, Exception
                ):
                    straggler = await peek_task
                    if isinstance(straggler, _InboundMessage):
                        await self._queue.put(straggler)
                exc = dispatch_task.exception()
                if exc is not None:
                    log.exception(
                        "telegram_bridge_dispatch_failed",
                        error=str(exc),
                    )
                return
            # The peek won — a new message arrived mid-dispatch. Pull
            # it, drain any fast follow-ups on top of it, then cancel
            # the in-flight plan so we can rerun with the merged intent.
            new_parts: list[_InboundMessage] = []
            with contextlib.suppress(Exception):
                new_parts.append(peek_task.result())
            new_parts.extend(
                await self._coalesce_from_queue(FOLLOW_UP_COALESCE_WINDOW_S)
            )
            running_id = self._orchestrator.running_plan_id
            if running_id is not None:
                log.info(
                    "telegram_bridge_follow_up_interrupt",
                    plan_id=running_id,
                    pending=len(new_parts),
                )
                self._current_dispatch_cancelled_for_follow_up = True
                with contextlib.suppress(Exception):
                    await self._orchestrator.cancel_plan(
                        running_id, reason=CANCEL_REASON_FOLLOW_UP
                    )
            # Wait bounded for the cancel to actually land so we don't
            # start the next run on top of a still-running one (the
            # orchestrator's lock would reject us anyway, but a clean
            # handoff avoids that noise).
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(
                    dispatch_task, timeout=DEFAULT_CANCEL_WAIT_S
                )
            # If the dispatch didn't wind down in time, cancel the
            # task as a last resort so we don't leak it.
            if not dispatch_task.done():
                dispatch_task.cancel()
                with contextlib.suppress(
                    asyncio.CancelledError, Exception
                ):
                    await dispatch_task
            # Merge the original intent on top of the follow-up so
            # PILK still sees what the operator was asking for, plus
            # the correction. The cancellation notice itself never
            # reaches Telegram — the listener inside _dispatch swallows
            # it when self._current_dispatch_cancelled_for_follow_up
            # is set.
            parts = parts + new_parts
            # Loop to redispatch.

    async def _dispatch(
        self, text: str, attachment_ids: list[str] | None = None,
    ) -> None:
        """Run one (possibly coalesced) message through the orchestrator
        and ship the reply back over Telegram.

        Prepends a rolling conversation-history preamble to ``text`` so
        PILK stays in-context across turns instead of re-greeting
        mid-thread. Waits a bounded amount of time when the
        orchestrator is busy rather than dropping the message.

        Ticks the session tracker on entry so every dispatch belongs
        to exactly one session — rapid-fire batches coalesced upstream
        still count as a single session tick because they arrive here
        as one call.

        ``attachment_ids`` references entries already saved into the
        chat ``AttachmentStore`` by the photo/document download path.
        We resolve them to ``ChatAttachment`` records here, just
        before the orchestrator call, so the run sees them as a
        first-class image / document / text content block.
        """
        now = datetime.now(UTC)
        session_id, is_new_session = self._session.tick(now)
        if is_new_session:
            log.info(
                "telegram_session_opened",
                session_id=session_id,
                chat_id=self._cfg.chat_id,
            )
        self._save_state()

        captured: dict[str, Any] = {}

        async def listener(event_type: str, payload: dict[str, Any]) -> None:
            if event_type != "chat.assistant":
                return
            if "text" in captured:
                return
            captured["text"] = payload.get("text") or ""

        prompt = _compose_prompt(list(self._history), text)

        attachments: list[ChatAttachment] = []
        if attachment_ids and self._attachment_store is not None:
            try:
                resolved = self._attachment_store.resolve_many(
                    list(attachment_ids),
                )
                attachments = [
                    ChatAttachment(
                        id=a.id,
                        kind=a.kind,
                        mime=a.mime,
                        filename=a.filename,
                        path=a.path,
                    )
                    for a in resolved
                ]
            except AttachmentError as e:
                log.warning(
                    "telegram_attachment_resolve_failed", error=str(e),
                )

        self._hub.subscribe(listener)
        try:
            ran = await self._run_with_busy_retry(prompt, attachments)
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

        # Supervisor cancelled this dispatch to fold in a follow-up
        # message. The orchestrator emits a ``chat.assistant: "Task
        # cancelled: operator follow-up — merging"`` notice; we
        # deliberately swallow it and skip history + persist so the
        # operator only ever sees the final merged reply, not a
        # confusing "cancelled" preamble for their own correction.
        if self._current_dispatch_cancelled_for_follow_up:
            log.info(
                "telegram_bridge_dispatch_cancelled_for_follow_up",
                session_id=session_id,
            )
            return

        reply = captured.get("text") or "(no response)"
        await self._safe_send(reply)

        # Only record the exchange once PILK actually replied — a
        # busy-exhausted or timed-out dispatch shouldn't pollute
        # history with a half-turn the model never saw.
        self._history.append(("user", text))
        self._history.append(("assistant", reply))
        await self._persist_exchange(
            text, reply,
            session_id=session_id,
            is_new_session=is_new_session,
        )

    async def _run_with_busy_retry(
        self,
        prompt: str,
        attachments: list[ChatAttachment] | None = None,
    ) -> Any:
        """Call ``orchestrator.run`` with a bounded busy-retry loop.

        Returns ``None`` on success, the sentinel ``_BusyExhausted``
        when the retry budget is spent, ``_Timeout`` on
        ``TimeoutError``, or the raised exception on any other failure
        — the caller decides how to surface each to the operator.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._busy_retry_budget_s
        backoff = 0.75
        attachments = list(attachments or [])
        # Vision / document work needs a tier that supports image
        # input. LIGHT (gpt-4o-mini / Haiku) handles vision; we keep
        # the LIGHT pin even with attachments because Anthropic Tier
        # 1 ITPM caps still favour Haiku for first-turn Telegram
        # traffic. If a specific deployment hits image-resolution
        # ceilings we'll bump to STANDARD here.
        while True:
            try:
                await asyncio.wait_for(
                    # Pin Telegram conversational turns to LIGHT (Haiku).
                    # Anthropic Tier 1 only allows 30k ITPM for Sonnet,
                    # which a first-turn orchestrator prompt (system +
                    # tools + memory hydration) can exceed on its own.
                    # Haiku has a 50k ITPM cap and is plenty capable for
                    # the conversational use Telegram gets; specialist
                    # agents invoked from Telegram still honor their
                    # manifest preferred_tier pins.
                    self._orchestrator.run(
                        prompt,
                        attachments=attachments,
                        preferred_tier="light",
                    ),
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
        self,
        user_text: str,
        assistant_text: str,
        *,
        session_id: str,
        is_new_session: bool,
    ) -> None:
        """Persist one exchange to the vault in three shapes.

        * ``chats/telegram/YYYY-MM-DD.md`` — daily digest the
          operator browses in Obsidian.
        * ``ingested/telegram/YYYY-MM-DD-HH.md`` — per-hour file
          the memory hydration + brain search layers pick up as
          recent context on subsequent turns.
        * ``sessions/telegram/{session_id}.md`` — one file per
          conversation session, surfaces in the Brain → Sessions
          category as a complete readable thread. The first
          exchange in a session writes a short header with the
          start time; later exchanges just append an exchange
          block.

        Silent-fail by design — a vault write error must never crash
        the bridge or block the reply the operator is waiting on.
        """
        # State (including the just-appended history turn) must be
        # flushed even when no vault is wired — otherwise a restart
        # loses the rolling conversation window for operators running
        # without a brain vault.
        if self._vault is None:
            self._save_state()
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
        session_rel = f"{SESSION_LOG_FOLDER}/{session_id}.md"
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
        try:
            session_header = _session_file_header(
                session_id=session_id,
                started_at=self._session.started_at or now,
            )
            await self._append_or_create(
                session_rel,
                block,
                header=session_header,
                force_header=is_new_session,
            )
        except Exception as e:
            log.warning(
                "telegram_bridge_session_log_failed",
                session_id=session_id, error=str(e),
            )
        # Persist session state so a daemon restart mid-conversation
        # rejoins the same session instead of silently forking one.
        self._save_state()

    async def _append_or_create(
        self,
        rel: str,
        block: str,
        *,
        header: str,
        force_header: bool = False,
    ) -> None:
        """Idempotently append ``block`` to ``rel`` in the vault.

        Reads first to learn if the file exists; the vault's ``read``
        raises FileNotFoundError when the file is missing, which we
        turn into a fresh write with the given ``header`` as a
        preamble. Subsequent writes append to the same file. When
        ``force_header`` is true, we always include the header — used
        by the session logger on the first message of a session so
        the per-session file always opens with its metadata banner.
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
        if exists and not force_header:
            await asyncio.to_thread(
                self._vault.write, rel, block, append=True,
            )
        else:
            await asyncio.to_thread(
                self._vault.write, rel, header + block,
            )

    # ── state persistence ──────────────────────────────────────────

    def _load_state(self) -> None:
        """Rehydrate offset, session tracker, and rolling conversation
        history from the persisted state file.

        Split out of ``start()`` so tests can exercise the
        rehydration path without spawning the network-polling
        background tasks. Safe to call multiple times; each call
        overwrites whatever is currently in memory.
        """
        state = _read_state(self._state_path)
        offset = state.get("offset")
        self._offset = int(offset) if isinstance(offset, int) else None
        session_state = state.get("session")
        if isinstance(session_state, dict):
            self._session.load_state(session_state)
        # Rehydrate the rolling conversation window so a daemon
        # restart mid-thread doesn't wipe PILK's short-term memory of
        # what was just said. The deque's ``maxlen`` naturally trims
        # anything beyond the window if a future version shrinks it.
        self._history.clear()
        history_raw = state.get("history")
        if isinstance(history_raw, list):
            for item in history_raw:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                text = item.get("text")
                if (
                    isinstance(role, str)
                    and role in ("user", "assistant")
                    and isinstance(text, str)
                ):
                    self._history.append((role, text))

    def _save_state(self) -> None:
        """Flush the offset + session tracker + rolling history to disk.

        The rolling history is what keeps PILK's short-term memory
        coherent across daemon restarts — without it, restarting pilkd
        mid-conversation drops the operator into a "fresh chat" that
        can't remember anything they just said. Each turn is capped at
        ``HISTORY_TURN_CHAR_CAP`` on the way out so the state file
        stays bounded even if someone pasted a 50 KB message earlier.

        Best-effort: a failed write is logged but never bubbles up —
        the bridge keeps running with in-memory state.
        """
        history_payload: list[dict[str, str]] = []
        for role, text in self._history:
            body = text or ""
            if len(body) > HISTORY_TURN_CHAR_CAP:
                body = body[:HISTORY_TURN_CHAR_CAP]
            history_payload.append({"role": role, "text": body})
        _write_state(
            self._state_path,
            {
                "offset": self._offset,
                "session": self._session.as_state(),
                "history": history_payload,
            },
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


# ── bridge state persistence ─────────────────────────────────────


def _read_state(path: Path) -> dict[str, Any]:
    """Load the bridge's persisted state.

    Shape: ``{"offset": int, "session": {...}}``. Missing file or
    malformed JSON returns an empty dict so callers can treat the
    result uniformly. Backwards-compatible with the pre-session file
    format (``{"offset": 42}``) — the absent ``session`` key just
    leaves the tracker in its fresh-init state.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        log.warning("telegram_bridge_state_read_failed", error=str(e))
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError as e:
        log.warning("telegram_bridge_state_write_failed", error=str(e))


def _session_file_header(*, session_id: str, started_at: datetime) -> str:
    """Banner written at the top of each per-session vault file.

    Gives the operator a glanceable header when they open the note
    in Obsidian, and gives memory hydration enough metadata to tag
    ingested exchanges with their session context.
    """
    pretty = started_at.strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"# Session {session_id}\n\n"
        f"- **Channel:** Telegram\n"
        f"- **Started:** {pretty}\n\n"
        f"---\n\n"
    )


__all__ = [
    "CHAT_LOG_FOLDER",
    "DEFAULT_BUSY_RETRY_BUDGET_S",
    "DEFAULT_COALESCE_WINDOW_S",
    "DEFAULT_LONGPOLL_S",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "DEFAULT_SESSION_IDLE_GAP_S",
    "HISTORY_MAX_TURNS",
    "HISTORY_TURN_CHAR_CAP",
    "INGEST_LOG_FOLDER",
    "ORCHESTRATOR_WAIT_TIMEOUT_S",
    "RETRY_BACKOFF_S",
    "SESSION_LOG_FOLDER",
    "TelegramBridge",
]
