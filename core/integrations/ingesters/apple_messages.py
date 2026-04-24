"""Apple Messages ingester — pull local iMessage / SMS history into
the brain vault as per-contact markdown notes.

Walks ``~/Library/Messages/chat.db`` (read-only, FDA-gated), groups
messages by their 1:1 chat, and renders one note per conversation
under ``ingested/messages/<handle>.md``. Group chats and noisy
numeric-only senders (typical of verification-code shortcodes) are
skipped by default so the vault stays useful.

Dates in chat.db are Apple "mac absolute time" — nanoseconds since
2001-01-01T00:00:00Z. Older macOS versions stored seconds; we detect
by magnitude.

macOS Ventura+ stopped populating the plain ``text`` column for many
messages; the real body lives in ``attributedBody`` — a binary
typedstream blob holding an ``NSMutableAttributedString``. We
byte-scan for the embedded NSString payload rather than depending on
a full typedstream decoder. It's a best-effort recovery; messages
whose blobs don't contain a decodable NSString still fall through to
the non-text skip path.

The ingester is pure: it returns ``IngestedNote`` instances. The tool
layer (``core/tools/builtin/brain_ingest.py``) handles vault writes.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.integrations.ingesters import IngestedNote
from core.logging import get_logger

log = get_logger("pilkd.ingest.apple_messages")

DEFAULT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"
MAC_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)

DEFAULT_SINCE_DAYS = 90
HARD_MAX_THREADS = 2_000
HARD_MAX_MESSAGES_PER_THREAD = 5_000

# Group chat style from chat.style column (Messages internal constant).
_STYLE_GROUP = 43
# Handles that look like shortcode verification senders — purely
# numeric sender ID, usually 3-7 digits, no ``@`` or ``+``.
_SHORTCODE_RE = re.compile(r"^\d{3,7}$")


class AppleMessagesIngestError(ValueError):
    """Operator-visible validation problem (chat.db missing, FDA not
    granted, schema mismatch). Per-thread failures go to the scan
    result's ``skipped`` list instead."""


@dataclass(frozen=True)
class MessageRow:
    at: datetime
    from_me: bool
    handle: str        # the other party's id; "" when the DB row is
                       # orphaned (shouldn't happen for DMs)
    text: str


@dataclass(frozen=True)
class ConversationThread:
    chat_id: int
    handle: str          # 1:1 chats only — the single other-party handle
    display_name: str    # contact name if Messages has one, else handle
    messages: list[MessageRow]
    last_at: datetime

    @property
    def note_slug(self) -> str:
        """Filename-safe slug built from display_name + handle."""
        base = self.display_name or self.handle or f"chat-{self.chat_id}"
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-")
        return slug or f"chat-{self.chat_id}"


@dataclass(frozen=True)
class ScanResult:
    db_path: Path
    since: datetime
    threads: list[ConversationThread]
    skipped: list[tuple[str, str]]    # (identifier, reason)


# ── public surface ──────────────────────────────────────────────────


def scan_apple_messages(
    *,
    since_days: int = DEFAULT_SINCE_DAYS,
    include_groups: bool = False,
    skip_shortcodes: bool = True,
    max_threads: int = HARD_MAX_THREADS,
    db_path: Path | None = None,
) -> ScanResult:
    """Read chat.db and return DM threads with any activity in the
    last ``since_days`` days.

    Scopes are conservative by default — DMs only, shortcodes skipped.
    Flip ``include_groups`` / ``skip_shortcodes`` if the operator
    wants the full firehose.
    """
    path = (db_path or DEFAULT_DB_PATH).expanduser()
    if not path.exists():
        raise AppleMessagesIngestError(
            f"chat.db not found at {path}. Apple Messages ingest only "
            "works on the Mac whose Messages.app is signed in."
        )
    since_days = max(1, min(since_days, 3650))  # up to ~10y cap
    since = datetime.now(UTC) - timedelta(days=since_days)
    since_apple_ns = int((since - MAC_EPOCH).total_seconds() * 1e9)

    try:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro&immutable=1", uri=True
        )
    except sqlite3.OperationalError as e:
        raise AppleMessagesIngestError(
            f"Could not open {path} read-only ({e}). Grant Full Disk "
            "Access to the Python process in System Settings → "
            "Privacy & Security → Full Disk Access, then retry."
        ) from e

    threads: list[ConversationThread] = []
    skipped: list[tuple[str, str]] = []
    try:
        conn.row_factory = sqlite3.Row
        # Find every chat with at least one message in the window.
        # style != 43 filters out groups when include_groups is False.
        style_filter = "" if include_groups else "AND (c.style IS NULL OR c.style != 43)"
        chat_rows = conn.execute(
            f"""
            SELECT
              c.ROWID AS chat_id,
              c.display_name AS display_name,
              c.chat_identifier AS chat_identifier,
              c.style AS style,
              MAX(m.date) AS last_date,
              COUNT(m.ROWID) AS msg_count
            FROM chat c
            JOIN chat_message_join cm ON cm.chat_id = c.ROWID
            JOIN message m ON m.ROWID = cm.message_id
            WHERE m.date > ?
            {style_filter}
            GROUP BY c.ROWID
            ORDER BY last_date DESC
            """,
            (since_apple_ns,),
        ).fetchall()

        for chat in chat_rows:
            if len(threads) >= max_threads:
                skipped.append(
                    (f"chat:{chat['chat_id']}", "max_threads cap reached")
                )
                continue
            chat_id = int(chat["chat_id"])
            handle_row = conn.execute(
                """
                SELECT h.id AS handle_id
                FROM chat_handle_join chj
                JOIN handle h ON h.ROWID = chj.handle_id
                WHERE chj.chat_id = ?
                LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
            handle = (handle_row["handle_id"] if handle_row else "") or ""
            if skip_shortcodes and _is_shortcode(handle):
                skipped.append((handle, "numeric shortcode (verification codes)"))
                continue

            msg_rows = conn.execute(
                """
                SELECT m.date AS date, m.text AS text,
                       m.attributedBody AS attributed_body,
                       m.is_from_me AS is_from_me,
                       h.id AS handle_id
                FROM message m
                JOIN chat_message_join cm ON cm.message_id = m.ROWID
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                WHERE cm.chat_id = ? AND m.date > ?
                ORDER BY m.date ASC
                LIMIT ?
                """,
                (chat_id, since_apple_ns, HARD_MAX_MESSAGES_PER_THREAD),
            ).fetchall()

            messages: list[MessageRow] = []
            for r in msg_rows:
                text = (r["text"] or "").strip()
                if not text and r["attributed_body"]:
                    decoded = _extract_text_from_attributed_body(
                        bytes(r["attributed_body"])
                    )
                    if decoded:
                        text = decoded.strip()
                if not text:
                    continue
                messages.append(
                    MessageRow(
                        at=_mac_ns_to_dt(r["date"]),
                        from_me=bool(r["is_from_me"]),
                        handle=(r["handle_id"] or "") if not r["is_from_me"] else "",
                        text=text,
                    )
                )
            if not messages:
                # Threads whose only activity was non-text (stickers,
                # reactions) offer nothing useful to the brain yet.
                skipped.append(
                    (handle or f"chat:{chat_id}", "no text messages in window")
                )
                continue

            display = (chat["display_name"] or "").strip() or handle or f"chat-{chat_id}"
            threads.append(
                ConversationThread(
                    chat_id=chat_id,
                    handle=handle,
                    display_name=display,
                    messages=messages,
                    last_at=messages[-1].at,
                )
            )
    finally:
        conn.close()

    return ScanResult(
        db_path=path, since=since, threads=threads, skipped=skipped
    )


def render_thread_note(thread: ConversationThread) -> IngestedNote:
    """Render one conversation thread as a brain vault markdown note."""
    path = f"ingested/messages/{thread.note_slug}.md"
    header = (
        f"# {thread.display_name}\n\n"
        f"- **Handle:** {thread.handle or '_unknown_'}\n"
        f"- **Chat ID:** {thread.chat_id}\n"
        f"- **Messages:** {len(thread.messages)}\n"
        f"- **Last activity:** {thread.last_at.isoformat(timespec='minutes')}\n"
        "\n---\n\n"
    )
    lines: list[str] = []
    last_date: str | None = None
    for m in thread.messages:
        date_str = m.at.strftime("%Y-%m-%d")
        if date_str != last_date:
            lines.append(f"\n### {date_str}\n")
            last_date = date_str
        speaker = "**me**" if m.from_me else f"**{thread.display_name}**"
        time_str = m.at.strftime("%H:%M")
        # Preserve newlines inside a single message by indenting
        # continuation lines so markdown treats them as one paragraph.
        body = m.text.replace("\n", "\n  ")
        lines.append(f"- `{time_str}` {speaker}: {body}")
    body = header + "\n".join(lines) + "\n"
    return IngestedNote(
        path=path,
        body=body,
        source_id=f"apple-messages:{thread.chat_id}",
        title=thread.display_name,
    )


# ── internals ───────────────────────────────────────────────────────


def _mac_ns_to_dt(value: int | None) -> datetime:
    if not value:
        return MAC_EPOCH
    v = int(value)
    seconds = v / 1e9 if v > 10**14 else float(v)
    return MAC_EPOCH + timedelta(seconds=seconds)


def _is_shortcode(handle: str) -> bool:
    if not handle:
        return False
    stripped = handle.lstrip("+").strip()
    return bool(_SHORTCODE_RE.match(stripped))


# Byte signature that precedes the NSString payload inside a Messages
# ``attributedBody`` typedstream blob. Stable across macOS 13-15: the
# stream emits NSString's class reference (0x94 0x84 0x01) followed by
# ``+`` (0x2b) which kicks off the string's length-prefixed UTF-8.
_NSSTRING_MARKER = b"\x94\x84\x01+"


def _extract_text_from_attributed_body(blob: bytes) -> str | None:
    """Best-effort extract of the user-visible message text from a
    Messages.app typedstream blob.

    Not a full NSKeyedArchiver / typedstream parser — we byte-scan for
    the NSString class marker then read the length-prefixed UTF-8
    payload that follows. Apple uses a compact variable-length
    encoding: a single byte ``< 0x81`` is the length directly;
    ``0x81 ll ll`` (little-endian u16) and ``0x82 ll ll ll ll`` (u32)
    are the extended forms.
    """
    idx = blob.find(_NSSTRING_MARKER)
    if idx < 0:
        return None
    pos = idx + len(_NSSTRING_MARKER)
    if pos >= len(blob):
        return None
    lead = blob[pos]
    if lead == 0x81:
        if pos + 3 > len(blob):
            return None
        length = int.from_bytes(blob[pos + 1 : pos + 3], "little")
        start = pos + 3
    elif lead == 0x82:
        if pos + 5 > len(blob):
            return None
        length = int.from_bytes(blob[pos + 1 : pos + 5], "little")
        start = pos + 5
    else:
        length = lead
        start = pos + 1
    end = start + length
    if end > len(blob) or length == 0:
        return None
    try:
        return blob[start:end].decode("utf-8")
    except UnicodeDecodeError:
        # Fall back to replacement mode — better than dropping the msg.
        return blob[start:end].decode("utf-8", errors="replace")


__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_SINCE_DAYS",
    "HARD_MAX_THREADS",
    "AppleMessagesIngestError",
    "ConversationThread",
    "MessageRow",
    "ScanResult",
    "render_thread_note",
    "scan_apple_messages",
]
