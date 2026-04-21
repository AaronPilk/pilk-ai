"""Read-only access to Messages.app's local SQLite database.

Messages.app stores iMessage + SMS/RCS history at:

    ~/Library/Messages/chat.db

We open this file read-only through `sqlite3`, which means:

- The Python process needs macOS Full Disk Access (granted in
  System Settings → Privacy & Security) to read it without an
  `authorization required` error. PILK surfaces the failure cleanly
  when that's not granted yet.
- It's a live DB. We open in URI mode with `mode=ro` and
  `immutable=1` to be explicit that we are not writing.
- It works only when PILK is running on the Mac whose Messages
  account is active. Hosted/remote PILK has no way to read this.

Schema we rely on:

  handle(id, ROWID)                  per-contact handle (phone / email)
  chat(ROWID, display_name, chat_identifier, style)
                                     a thread
  chat_handle_join(chat_id, handle_id)
  chat_message_join(chat_id, message_id)
  message(ROWID, text, attributedBody, date, is_from_me,
          handle_id, cache_has_attachments)
                                     individual messages; `date` is
                                     mac-epoch nanoseconds (since
                                     2001-01-01T00:00:00Z).

Dates in this DB are Apple's "mac absolute time" — nanoseconds since
2001-01-01. We convert to UTC ISO strings at the edges.

`attributedBody` is a binary plist that sometimes replaces `text` for
RichLink and similar payloads. We fall back to a placeholder when we
only have that shape — decoding it requires NSKeyedArchiver logic
out of scope for this batch.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.apple.messages")

DEFAULT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"
# Mac Absolute Time epoch — Apple's reference date.
MAC_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)

MAX_THREAD_PREVIEW = 5
MAX_THREAD_SNIPPET = 120
MAX_THREAD_MESSAGES = 30
MAX_SEARCH_RESULTS = 25
# Hard cap on outbound message body. Messages.app itself accepts very
# long bodies, but a 50k-character prompt is almost certainly a bug on
# the orchestrator side; we stop that before it turns into the
# operator's SMS bill.
MAX_OUTBOUND_CHARS = 2000
# How long we let osascript run before hard-killing it. The actual
# sendMessage call returns fast; anything past this means Messages is
# hung on a Continuity handoff or is prompting for permission.
OSASCRIPT_TIMEOUT_S = 10.0


@dataclass
class MessagesStatus:
    available: bool
    db_path: str
    reason: str | None = None

    def to_public(self) -> dict:
        return asdict(self)


def db_path_for(home: Path | None = None) -> Path:
    """Allow the tests (and future hosted mode) to point at a different
    chat.db. The env var only exists so test fixtures can swap in a
    synthetic DB; production uses the default macOS path.
    """
    override = os.getenv("PILK_APPLE_MESSAGES_DB")
    if override:
        return Path(override)
    return DEFAULT_DB_PATH


def check_messages_status() -> MessagesStatus:
    path = db_path_for()
    if not path.exists():
        return MessagesStatus(
            available=False,
            db_path=str(path),
            reason=(
                "chat.db not found at this path. Apple Messages reading "
                "only works on the Mac whose Messages.app is signed in."
            ),
        )
    try:
        conn = _open_ro(path)
    except sqlite3.OperationalError as e:
        reason = str(e)
        # authorization required = Full Disk Access not granted yet.
        hint = (
            "Grant Full Disk Access to the Python process in System "
            "Settings → Privacy & Security → Full Disk Access, then "
            "restart pilkd."
            if "authorization" in reason.lower()
            else "chat.db exists but couldn't be opened."
        )
        return MessagesStatus(
            available=False,
            db_path=str(path),
            reason=f"{hint} ({reason})",
        )
    try:
        # Cheap sanity check — pragma is harmless + confirms schema.
        conn.execute("SELECT COUNT(*) FROM message")
    finally:
        conn.close()
    return MessagesStatus(available=True, db_path=str(path))


# ── read helpers ─────────────────────────────────────────────────────────


def recent_threads(limit: int = MAX_THREAD_PREVIEW) -> list[dict]:
    """Return the most recently-active threads with a preview snippet.

    Used by the Home MessagesCard. Read-only, no writes.
    """
    path = db_path_for()
    if not path.exists():
        return []
    conn = _open_ro(path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
              c.ROWID AS chat_id,
              c.display_name AS display_name,
              c.chat_identifier AS chat_identifier,
              c.style AS style,
              MAX(m.date) AS last_date,
              (
                SELECT text FROM message m2
                JOIN chat_message_join cm2 ON cm2.message_id = m2.ROWID
                WHERE cm2.chat_id = c.ROWID
                ORDER BY m2.date DESC LIMIT 1
              ) AS last_text,
              (
                SELECT is_from_me FROM message m3
                JOIN chat_message_join cm3 ON cm3.message_id = m3.ROWID
                WHERE cm3.chat_id = c.ROWID
                ORDER BY m3.date DESC LIMIT 1
              ) AS last_from_me
            FROM chat c
            JOIN chat_message_join cm ON cm.chat_id = c.ROWID
            JOIN message m ON m.ROWID = cm.message_id
            GROUP BY c.ROWID
            ORDER BY last_date DESC
            LIMIT ?
            """,
            (int(max(1, min(limit, 50))),),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict] = []
    for r in rows:
        snippet = _snippet(r["last_text"])
        out.append(
            {
                "chat_id": r["chat_id"],
                "title": _thread_title(r),
                "is_group": bool((r["style"] or 0) == 43),
                "last_at": _mac_date_to_iso(r["last_date"]),
                "last_snippet": snippet,
                "last_from_me": bool(r["last_from_me"] or 0),
            }
        )
    return out


def read_thread(chat_id: int, limit: int = MAX_THREAD_MESSAGES) -> dict:
    """Return the most recent N messages in a single thread."""
    path = db_path_for()
    if not path.exists():
        raise RuntimeError(f"chat.db not found at {path}")
    conn = _open_ro(path)
    try:
        conn.row_factory = sqlite3.Row
        chat = conn.execute(
            """
            SELECT ROWID, display_name, chat_identifier, style
            FROM chat WHERE ROWID = ?
            """,
            (int(chat_id),),
        ).fetchone()
        if chat is None:
            raise RuntimeError(f"no such chat: {chat_id}")
        rows = conn.execute(
            """
            SELECT
              m.ROWID AS message_id,
              m.date AS date,
              m.text AS text,
              m.is_from_me AS is_from_me,
              h.id AS handle_id
            FROM message m
            JOIN chat_message_join cm ON cm.message_id = m.ROWID
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE cm.chat_id = ?
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (int(chat_id), int(max(1, min(limit, MAX_THREAD_MESSAGES)))),
        ).fetchall()
    finally:
        conn.close()
    messages = [
        {
            "message_id": r["message_id"],
            "at": _mac_date_to_iso(r["date"]),
            "from": "me" if r["is_from_me"] else (r["handle_id"] or "unknown"),
            "text": r["text"] or "",
        }
        for r in reversed(rows)
    ]
    return {
        "chat_id": chat["ROWID"],
        "title": _thread_title(chat),
        "is_group": bool((chat["style"] or 0) == 43),
        "messages": messages,
    }


def search_messages(query: str, limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
    """Full-text-ish search against the message.text column.

    No FTS index in the stock chat.db, so we use LIKE. Fine for small
    inboxes; O(n) over messages on larger ones.
    """
    path = db_path_for()
    if not path.exists():
        return []
    like = f"%{query}%"
    conn = _open_ro(path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
              m.ROWID AS message_id,
              m.date AS date,
              m.text AS text,
              m.is_from_me AS is_from_me,
              cm.chat_id AS chat_id,
              c.display_name AS display_name,
              c.chat_identifier AS chat_identifier,
              c.style AS style,
              h.id AS handle_id
            FROM message m
            JOIN chat_message_join cm ON cm.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cm.chat_id
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE m.text LIKE ?
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (like, int(max(1, min(limit, MAX_SEARCH_RESULTS)))),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "message_id": r["message_id"],
            "chat_id": r["chat_id"],
            "title": _thread_title(r),
            "at": _mac_date_to_iso(r["date"]),
            "from": "me" if r["is_from_me"] else (r["handle_id"] or "unknown"),
            "snippet": _snippet(r["text"]),
        }
        for r in rows
    ]


# ── send (AppleScript out-of-process) ───────────────────────────────────


class MessagesSendError(RuntimeError):
    """Raised when osascript-driven Messages send fails. Wraps the
    underlying ``subprocess.CalledProcessError`` or friendly reason so
    callers get a single exception type to catch."""


# Parked at module level so tests can monkeypatch one seam and exercise
# every tool path without stubbing subprocess in each test.
def _run_osascript(script: str, *args: str) -> str:
    """Execute an AppleScript via ``osascript``.

    Script reads from stdin (``osascript -``) and we pass ``args`` as
    positional arguments — AppleScript's ``on run argv`` picks them up
    as strings. That lets us hand over the recipient + message body
    without AppleScript string-escaping hell: no quote-doubling, no
    backslash dance, no injection vector if the text contains
    ``"; do something evil"``.

    Raises :class:`MessagesSendError` on any non-zero exit. Swallows
    stdout so callers can keep their log lines tidy.
    """
    if sys.platform != "darwin":
        raise MessagesSendError(
            "Apple Messages send is macOS-only. Run PILK on the Mac "
            "whose Messages.app holds the account."
        )
    binary = shutil.which("osascript")
    if binary is None:
        raise MessagesSendError(
            "osascript binary not found. It ships with macOS; "
            "something is very wrong with this environment."
        )
    try:
        proc = subprocess.run(
            [binary, "-", *args],
            input=script,
            capture_output=True,
            text=True,
            timeout=OSASCRIPT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise MessagesSendError(
            f"osascript timed out after {OSASCRIPT_TIMEOUT_S}s — "
            "Messages.app may be asking for permission; grant "
            "Automation access to your terminal / pilkd process."
        ) from e
    except OSError as e:
        raise MessagesSendError(f"osascript failed to launch: {e}") from e
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        # Permission-denied errors from macOS have a recognisable
        # signature. Rewrite them into actionable copy so the operator
        # doesn't have to decode `-1743` codes.
        if "not authorised" in stderr.lower() or "-1743" in stderr:
            hint = (
                "macOS refused Automation access. Open System Settings "
                "→ Privacy & Security → Automation and allow your "
                "terminal / pilkd process to control Messages."
            )
        else:
            hint = stderr or f"osascript exit {proc.returncode}"
        raise MessagesSendError(hint)
    return (proc.stdout or "").strip()


def _resolve_recipient_for_chat(chat_id: int) -> str:
    """Look up the handle (phone or email) of a 1:1 chat so we can hand
    it to Messages.app's ``buddy`` selector.

    Group chats have multiple handles; for the V1 send path we refuse
    those rather than pick arbitrarily — the operator should name
    the recipient explicitly.
    """
    path = db_path_for()
    if not path.exists():
        raise MessagesSendError(f"chat.db not found at {path}")
    conn = _open_ro(path)
    try:
        conn.row_factory = sqlite3.Row
        chat = conn.execute(
            "SELECT ROWID, style FROM chat WHERE ROWID = ?",
            (int(chat_id),),
        ).fetchone()
        if chat is None:
            raise MessagesSendError(f"no such chat: {chat_id}")
        if (chat["style"] or 0) == 43:
            raise MessagesSendError(
                f"chat {chat_id} is a group; "
                "messages_send refuses group sends in V1. "
                "Pass phone_number to target a single recipient."
            )
        row = conn.execute(
            """
            SELECT h.id AS handle_id
            FROM chat_handle_join chj
            JOIN handle h ON h.ROWID = chj.handle_id
            WHERE chj.chat_id = ?
            LIMIT 1
            """,
            (int(chat_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row["handle_id"]:
        raise MessagesSendError(
            f"chat {chat_id} has no handle we can send to."
        )
    return str(row["handle_id"])


def send_message(recipient: str, text: str) -> None:
    """Send ``text`` to ``recipient`` (phone / email handle) via
    Messages.app. No-ops on non-macOS after raising a clear error.

    The recipient / text round-trip through ``argv`` so AppleScript
    sees them as opaque strings — no injection, no escaping.
    """
    script = (
        "on run argv\n"
        "    set targetRecipient to item 1 of argv\n"
        "    set messageText to item 2 of argv\n"
        "    tell application \"Messages\"\n"
        "        set targetService to first service whose "
        "service type is iMessage\n"
        "        set targetBuddy to buddy targetRecipient "
        "of targetService\n"
        "        send messageText to targetBuddy\n"
        "    end tell\n"
        "end run\n"
    )
    _run_osascript(script, recipient, text)


# ── tools ────────────────────────────────────────────────────────────────


def make_messages_tools() -> list[Tool]:
    async def _search(args: dict, ctx: ToolContext) -> ToolOutcome:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolOutcome(
                content="messages_search_mine requires a 'query'.",
                is_error=True,
            )
        try:
            limit = int(args.get("max_results") or MAX_SEARCH_RESULTS)
        except (TypeError, ValueError):
            limit = MAX_SEARCH_RESULTS
        try:
            results = await asyncio.to_thread(search_messages, query, limit)
        except Exception as e:
            log.exception("messages_search_failed")
            return ToolOutcome(
                content=(
                    f"messages_search_mine failed: {type(e).__name__}: {e}. "
                    "Grant Full Disk Access to the Python process if you "
                    "haven't already."
                ),
                is_error=True,
            )
        if not results:
            return ToolOutcome(
                content=f"No messages match {query!r}.",
                data={"query": query, "results": []},
            )
        lines = [
            f"{r['at'][:16]}  {r['title'][:40]:<40}  {r['snippet']}"
            for r in results
        ]
        header = f"{len(results)} match(es) for {query!r}:\n\n"
        return ToolOutcome(
            content=header + "\n".join(lines),
            data={"query": query, "results": results},
        )

    async def _read(args: dict, ctx: ToolContext) -> ToolOutcome:
        try:
            chat_id = int(args.get("chat_id"))
        except (TypeError, ValueError):
            return ToolOutcome(
                content="messages_read_thread requires an integer 'chat_id'.",
                is_error=True,
            )
        try:
            limit = int(args.get("max_messages") or 15)
        except (TypeError, ValueError):
            limit = 15
        try:
            thread = await asyncio.to_thread(read_thread, chat_id, limit)
        except Exception as e:
            log.exception("messages_read_thread_failed")
            return ToolOutcome(
                content=f"messages_read_thread failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        lines = [
            f"{m['at'][11:16]}  {m['from'][:24]:<24}  {m['text']}"
            for m in thread["messages"]
        ]
        header = (
            f"Thread: {thread['title']} "
            f"({'group' if thread['is_group'] else 'direct'})\n\n"
        )
        return ToolOutcome(content=header + "\n".join(lines), data=thread)

    async def _send(args: dict, ctx: ToolContext) -> ToolOutcome:
        text = str(args.get("text") or "").strip()
        if not text:
            return ToolOutcome(
                content="messages_send requires a non-empty 'text'.",
                is_error=True,
            )
        if len(text) > MAX_OUTBOUND_CHARS:
            return ToolOutcome(
                content=(
                    f"messages_send text too long "
                    f"({len(text)} > {MAX_OUTBOUND_CHARS}). Split into "
                    "multiple sends or send a document instead."
                ),
                is_error=True,
            )
        phone_number = str(args.get("phone_number") or "").strip()
        chat_id_raw = args.get("chat_id")
        if not phone_number and chat_id_raw is None:
            return ToolOutcome(
                content=(
                    "messages_send requires either 'chat_id' (from a "
                    "messages_search_mine result) or 'phone_number'."
                ),
                is_error=True,
            )
        if phone_number and chat_id_raw is not None:
            return ToolOutcome(
                content=(
                    "messages_send takes 'chat_id' OR 'phone_number', "
                    "not both. The operator should name the recipient "
                    "unambiguously."
                ),
                is_error=True,
            )
        # Resolve chat_id → handle string before we shell out.
        try:
            if phone_number:
                recipient = phone_number
            else:
                try:
                    chat_id = int(chat_id_raw)
                except (TypeError, ValueError):
                    return ToolOutcome(
                        content="messages_send 'chat_id' must be an integer.",
                        is_error=True,
                    )
                recipient = await asyncio.to_thread(
                    _resolve_recipient_for_chat, chat_id,
                )
        except MessagesSendError as e:
            return ToolOutcome(
                content=f"messages_send failed: {e}", is_error=True,
            )
        try:
            await asyncio.to_thread(send_message, recipient, text)
        except MessagesSendError as e:
            log.warning(
                "messages_send_failed",
                recipient_kind="phone" if phone_number else "chat_id",
                error=str(e),
            )
            return ToolOutcome(
                content=f"messages_send failed: {e}", is_error=True,
            )
        except Exception as e:
            log.exception("messages_send_unexpected_error")
            return ToolOutcome(
                content=f"messages_send failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=f"Sent to {recipient}: {text[:80]}",
            data={"recipient": recipient, "chars": len(text)},
        )

    search_tool = Tool(
        name="messages_search_mine",
        description=(
            "Search your local Apple Messages history by text fragment. "
            "READ risk — this is a local read; no network. Requires "
            "Full Disk Access granted to the Python process on macOS."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_SEARCH_RESULTS,
                },
            },
            "required": ["query"],
        },
        risk=RiskClass.READ,
        handler=_search,
    )
    read_tool = Tool(
        name="messages_read_thread",
        description=(
            "Read the most recent messages in a single Apple Messages "
            "thread, identified by `chat_id` (from messages_search_mine "
            "results or the Home tile). READ risk — local only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer"},
                "max_messages": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_THREAD_MESSAGES,
                },
            },
            "required": ["chat_id"],
        },
        risk=RiskClass.READ,
        handler=_read,
    )
    send_tool = Tool(
        name="messages_send",
        description=(
            "Send an iMessage (or SMS via iPhone Continuity) through "
            "the local Messages.app. Recipient is either a `chat_id` "
            "from a prior messages_search_mine result (1:1 chats only) "
            "OR a `phone_number` / email handle. COMMS risk — every "
            "send lands in the approval queue so the operator reviews "
            "the recipient + body before Messages fires. macOS-only; "
            "needs Automation permission granted to the pilkd process."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "integer",
                    "description": (
                        "Messages.app internal chat ROWID (from "
                        "messages_search_mine). 1:1 chats only — "
                        "groups are refused for safety. Mutually "
                        "exclusive with phone_number."
                    ),
                },
                "phone_number": {
                    "type": "string",
                    "description": (
                        "Raw phone number (E.164 preferred, e.g. "
                        "+15551234567) or email handle. Mutually "
                        "exclusive with chat_id."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": (
                        f"Message body. Hard cap "
                        f"{MAX_OUTBOUND_CHARS} chars."
                    ),
                },
            },
            "required": ["text"],
        },
        risk=RiskClass.COMMS,
        handler=_send,
    )
    return [search_tool, read_tool, send_tool]


# ── internals ────────────────────────────────────────────────────────────


def _open_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _mac_date_to_iso(mac_nanos: int | None) -> str:
    if not mac_nanos:
        return ""
    # Some rows store seconds, some store nanoseconds depending on macOS
    # version. Detect by magnitude and convert.
    value = int(mac_nanos)
    seconds = value / 1e9 if value > 10**14 else float(value)
    return (MAC_EPOCH + timedelta(seconds=seconds)).isoformat()


def _snippet(text: str | None) -> str:
    if not text:
        return "(non-text message)"
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= MAX_THREAD_SNIPPET:
        return collapsed
    return collapsed[: MAX_THREAD_SNIPPET - 1].rstrip() + "…"


def _thread_title(row) -> str:
    name = row["display_name"] or row["chat_identifier"] or "Unknown"
    return name if name.strip() else "Unknown"
