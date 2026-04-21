"""Gmail bulk ingester — pulls the operator's inbox into the brain.

Uses the operator's existing Google OAuth (the "user" role, same one
the inbox-triage agent reads). One markdown note per Gmail thread
lands under ``ingested/gmail/<YYYY-MM-DD>-<subject-slug>.md``. Dating
the filename makes Obsidian's file browser sort by date naturally.

### Shape

- ``scan_threads(creds, query, max_threads)`` — hits
  ``gmail.users().threads().list`` with a GAQL-style Gmail query
  (``newer_than:30d`` by default) + walks the returned IDs through
  ``threads().get`` to pull full bodies. Returns typed
  ``GmailThread`` objects newest-first.
- ``render_thread_note(thread)`` — assembles markdown: title,
  metadata header (participants, first/last message date, thread
  id), then one section per message with from/to/date + body.
- Tool handler + auto-ingest hook live in
  :mod:`core.tools.builtin.brain_ingest` so all brain ingesters
  share registration.

### Tolerances

- Non-UTF-8 bytes in message bodies are replaced rather than
  raising — some senders attach weird encodings Gmail doesn't fix.
- Messages missing ``payload.headers`` are skipped but don't kill
  the thread.
- A malformed single thread is logged + skipped so one corrupt
  message doesn't abort the whole bulk ingest.

### Scope

V1 ingests ``newer_than:30d`` by default. Older history + a
background "page through everything" walker land in follow-ups.
The soft cap (``DEFAULT_MAX_THREADS``) keeps first-boot ingests
from producing thousands of notes before the operator has seen a
single one.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from core.integrations.ingesters import IngestedNote
from core.logging import get_logger

log = get_logger("pilkd.ingest.gmail")

DEFAULT_QUERY = "newer_than:30d"
DEFAULT_MAX_THREADS = 100
MAX_MESSAGES_PER_THREAD = 40
MAX_BODY_CHARS = 4_000


@dataclass(frozen=True)
class GmailMessage:
    message_id: str
    from_: str
    to: str
    subject: str
    date: datetime | None
    body: str


@dataclass(frozen=True)
class GmailThread:
    thread_id: str
    subject: str
    messages: list[GmailMessage]

    @property
    def latest_at(self) -> datetime | None:
        for m in reversed(self.messages):
            if m.date is not None:
                return m.date
        return None

    @property
    def participants(self) -> list[str]:
        seen: dict[str, None] = {}
        for m in self.messages:
            for field in (m.from_, m.to):
                for addr in _split_addresses(field):
                    if addr and addr not in seen:
                        seen[addr] = None
        return list(seen.keys())


def scan_threads(
    creds: Any,
    *,
    query: str = DEFAULT_QUERY,
    max_threads: int = DEFAULT_MAX_THREADS,
) -> list[GmailThread]:
    """Pull up to ``max_threads`` threads matching ``query`` from the
    operator's inbox. Returns newest-first (by latest message
    timestamp)."""
    service = creds.build("gmail", "v1")
    listing = (
        service.users()
        .threads()
        .list(userId="me", q=query, maxResults=max(1, int(max_threads)))
        .execute()
    )
    ids = [t.get("id") for t in (listing.get("threads") or []) if t.get("id")]
    out: list[GmailThread] = []
    for tid in ids:
        try:
            parsed = _fetch_thread(service, tid)
        except Exception as e:  # single-thread failure must not abort
            log.warning("gmail_ingest_thread_failed", thread_id=tid, error=str(e))
            continue
        if parsed is not None:
            out.append(parsed)
    out.sort(
        key=lambda t: t.latest_at or datetime.fromtimestamp(0, tz=UTC),
        reverse=True,
    )
    return out


def _fetch_thread(service: Any, thread_id: str) -> GmailThread | None:
    raw = (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="full")
        .execute()
    )
    raw_messages = raw.get("messages") or []
    if not raw_messages:
        return None
    # Cap messages per thread — a 200-deep reply chain from a
    # newsletter doesn't belong in the brain.
    window = raw_messages[-MAX_MESSAGES_PER_THREAD:]
    parsed: list[GmailMessage] = []
    subject = ""
    for m in window:
        payload = m.get("payload") or {}
        headers = {
            h["name"]: h.get("value", "")
            for h in (payload.get("headers") or [])
            if isinstance(h, dict)
        }
        if not subject:
            subject = headers.get("Subject", "(no subject)") or "(no subject)"
        body = _extract_text(payload) or m.get("snippet") or ""
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS].rstrip() + "\n\n… [truncated]"
        parsed.append(
            GmailMessage(
                message_id=str(m.get("id") or ""),
                from_=headers.get("From", ""),
                to=headers.get("To", ""),
                subject=headers.get("Subject", subject),
                date=_parse_gmail_date(headers.get("Date", "")),
                body=body,
            )
        )
    if not parsed:
        return None
    return GmailThread(
        thread_id=thread_id,
        subject=subject or "(no subject)",
        messages=parsed,
    )


# ── body extraction ────────────────────────────────────────────


def _extract_text(payload: dict[str, Any]) -> str:
    """Prefer text/plain; fall back to an HTML part with tags
    stripped. Handles the nested-multipart case recursively."""
    if not isinstance(payload, dict):
        return ""
    if payload.get("mimeType") == "text/plain":
        return _decode_body(payload.get("body") or {})
    parts = payload.get("parts") or []
    for part in parts:
        if part.get("mimeType") == "text/plain":
            return _decode_body(part.get("body") or {})
        if part.get("parts"):
            nested = _extract_text(part)
            if nested:
                return nested
    for part in parts:
        if part.get("mimeType") == "text/html":
            return _strip_html(_decode_body(part.get("body") or {}))
    return ""


def _decode_body(body: dict[str, Any]) -> str:
    data = body.get("data")
    if not isinstance(data, str):
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (ValueError, TypeError):
        return ""
    return raw.decode("utf-8", errors="replace")


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_NBSP = re.compile(r"&nbsp;")
_HTML_ENTITY_AMP = re.compile(r"&amp;")
_HTML_ENTITY_LT = re.compile(r"&lt;")
_HTML_ENTITY_GT = re.compile(r"&gt;")
_HTML_WHITESPACE = re.compile(r"[ \t]+\n|\n{3,}")


def _strip_html(html: str) -> str:
    """Poor-man's HTML → plain text. Good enough for email — we
    don't need a full DOM parse; the brain vault is human-readable
    regardless."""
    text = _HTML_TAG_RE.sub("", html)
    text = _HTML_ENTITY_NBSP.sub(" ", text)
    text = _HTML_ENTITY_AMP.sub("&", text)
    text = _HTML_ENTITY_LT.sub("<", text)
    text = _HTML_ENTITY_GT.sub(">", text)
    return _HTML_WHITESPACE.sub("\n\n", text).strip()


# ── date + address helpers ─────────────────────────────────────


def _parse_gmail_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


_ADDR_SPLIT_RE = re.compile(r"\s*,\s*")


def _split_addresses(raw: str) -> list[str]:
    if not raw:
        return []
    return [a for a in _ADDR_SPLIT_RE.split(raw) if a]


# ── note rendering ─────────────────────────────────────────────


_SAFE_STEM = re.compile(r"[^a-z0-9 _\-().']+")
STEM_MAX_CHARS = 60


def _safe_stem(raw: str, fallback: str = "thread") -> str:
    low = (raw or "").lower().replace("/", "-")
    low = _SAFE_STEM.sub("-", low)
    trimmed = low.strip("-")
    if not trimmed:
        return fallback
    return trimmed[:STEM_MAX_CHARS]


def render_thread_note(thread: GmailThread) -> IngestedNote:
    """Assemble one markdown note per thread. Structure:

    ```
    # <subject>

    _Gmail thread · thread_id `...`_

    - Latest: 2026-04-19T10:00:00Z
    - Messages: 3
    - Participants: alice@, bob@, ...

    ### From — at
    body

    ### From — at
    body
    ```
    """
    date_prefix = (
        thread.latest_at
        or datetime.fromtimestamp(0, tz=UTC)
    ).strftime("%Y-%m-%d")
    stem = _safe_stem(thread.subject, fallback="thread")
    header = [
        f"# {thread.subject}",
        "",
        f"_Gmail thread · thread_id `{thread.thread_id}`_",
        "",
        f"- Latest: {thread.latest_at.isoformat() if thread.latest_at else 'unknown'}",
        f"- Messages: {len(thread.messages)}",
        f"- Participants: {', '.join(thread.participants) or 'unknown'}",
        "",
    ]
    chunks: list[str] = []
    for m in thread.messages:
        when = m.date.isoformat() if m.date else "unknown"
        chunks.append(f"### {m.from_ or '(unknown sender)'} — {when}")
        if m.to:
            chunks.append(f"_to: {m.to}_")
        chunks.append("")
        chunks.append(m.body or "_(no body)_")
        chunks.append("")
    return IngestedNote(
        path=f"ingested/gmail/{date_prefix}-{stem}.md",
        body="\n".join(header + chunks),
        source_id=thread.thread_id,
        title=thread.subject,
    )


__all__ = [
    "DEFAULT_MAX_THREADS",
    "DEFAULT_QUERY",
    "GmailMessage",
    "GmailThread",
    "render_thread_note",
    "scan_threads",
]
