"""ChatGPT vault index — fast keyword + topic lookup over imported notes.

Once a ChatGPT export has been ingested into the vault (PR #93 / #94),
there can be thousands of per-conversation notes under
``ingested/chatgpt/``. The orchestrator's context builder wants to
pull a handful of *relevant* past conversations on every operator
message so PILK can reference the history without a full-text search
over the whole vault on every turn.

This module maintains a tiny JSONL side-index at
``ingested/chatgpt/_index.jsonl`` with one line per imported note::

    {
      "path":    "ingested/chatgpt/2024-01-05-tampa-kitchen-remodel.md",
      "title":   "Tampa kitchen remodel",
      "preview": "...first 300 chars of body...",
      "topic":   "brand",
      "mtime":   "2024-01-05T12:34:56+00:00",
      "size":    4120
    }

Topics are assigned by a cheap keyword matcher (``trading`` / ``brand``
/ ``business`` / ``personal`` / ``tech`` / ``general``) — same tag set
the Chat Archive UI surfaces. An LLM-enrichment pass can be added
later without changing the index schema; the field is a plain string.

The builder is safe to call concurrently (writes to a tempfile and
renames). Missing notes between list + read are tolerated silently —
a note that vanished just gets skipped.

Query is a plain weighted keyword match: hits in the title count 3x,
hits in the preview 1x. Ties break on recent mtime. Good enough for
tens of thousands of notes on a laptop; if we ever need smarter
recall, this is the only module to replace.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from core.logging import get_logger

log = get_logger("pilkd.brain.chatgpt_index")


# ── paths ────────────────────────────────────────────────────────────

CHATGPT_DIR = "ingested/chatgpt"
INDEX_FILE = "ingested/chatgpt/_index.jsonl"

# Preview length cap. 300 chars lines up with what the UI card shows
# and is plenty for keyword scoring without bloating the index on
# disk (roughly 500 bytes per entry at 300 preview chars → a 10k-note
# index is ~5 MiB, trivial to mmap).
PREVIEW_CHARS = 1500
TITLE_MAX_CHARS = 160
# How much of each conversation body we feed the topic classifier.
# Classifying on the preview alone (the first 300 chars) was missing
# most threads because ChatGPT conversations usually open with
# pleasantries / context-setting; the actual keywords (``trade``,
# ``pitch``, ``revenue``, ``logo``) show up later. Reading a larger
# window catches those without blowing memory on multi-megabyte
# exports.
CLASSIFY_CHARS = 8000

# YAML frontmatter at the top of notes (``---\n…\n---``). Stripped
# before title / preview extraction so the index doesn't leak
# frontmatter keys as titles.
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)
_HEADING_LINE_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
_INLINE_MD_RE = re.compile(r"[*_`]+")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


# ── topics ───────────────────────────────────────────────────────────

#: Ordered list of (topic_key, keyword_regex, ui_label). Ordering is
#: meaningful: the first topic whose keyword hits wins. We put the
#: more specific topics first (trading, brand, business) so broad
#: ones (tech, personal) don't swallow them.
_TOPIC_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "trading",
        re.compile(
            r"\b(xauusd|gold|forex|trade|trading|chart|candle|pip|entry|stop[\s-]*loss|"
            r"take[\s-]*profit|scalp|swing|long\b|short\b|bullish|bearish)\b",
            re.IGNORECASE,
        ),
        "Trading",
    ),
    (
        "brand",
        re.compile(
            r"\b(nv|brand|branding|logo|packaging|label|palette|mood[\s-]*board|"
            r"typography|typeface|visual\s+identity)\b",
            re.IGNORECASE,
        ),
        "Brand Building",
    ),
    (
        "business",
        re.compile(
            r"\b(pitch|revenue|offer|saas|startup|funnel|lead|mrr|arr|"
            r"pricing|go[\s-]*to[\s-]*market|gtm|churn|business\s+plan)\b",
            re.IGNORECASE,
        ),
        "Business Ideas",
    ),
    (
        "personal",
        re.compile(
            r"\b(health|relationship|relationships|mindset|diet|workout|"
            r"journal|meditat|therapy|personal)\b",
            re.IGNORECASE,
        ),
        "Personal",
    ),
    (
        "tech",
        re.compile(
            r"\b(code|coding|api|deploy|agent|python|javascript|typescript|"
            r"react|fastapi|kubernetes|docker|build\s+me|refactor|implement)\b",
            re.IGNORECASE,
        ),
        "Tech / Dev",
    ),
]

TOPIC_LABELS: dict[str, str] = {key: label for key, _, label in _TOPIC_RULES}
TOPIC_LABELS["general"] = "General"


def classify_topic(text: str) -> str:
    """Return the topic key (``trading`` / ``brand`` / …) for ``text``.

    Falls back to ``general`` when nothing matches. Pure keyword
    match; no LLM call. Ordered so specific wins over broad.
    """
    if not text:
        return "general"
    for key, rx, _label in _TOPIC_RULES:
        if rx.search(text):
            return key
    return "general"


# ── extraction ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class IndexEntry:
    """One row of the ChatGPT index."""

    path: str
    title: str
    preview: str
    topic: str
    mtime: str
    size: int

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "path": self.path,
                "title": self.title,
                "preview": self.preview,
                "topic": self.topic,
                "mtime": self.mtime,
                "size": self.size,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json_line(cls, raw: str) -> IndexEntry | None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        try:
            return cls(
                path=str(data["path"]),
                title=str(data.get("title") or ""),
                preview=str(data.get("preview") or ""),
                topic=str(data.get("topic") or "general"),
                mtime=str(data.get("mtime") or ""),
                size=int(data.get("size") or 0),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _derive_title(body: str, fallback: str) -> str:
    """First heading → first prose line → fallback (the filename stem)."""
    clean = _FRONTMATTER_RE.sub("", body or "", count=1)
    for line in clean.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("---", ">", "|")):
            continue
        h = _HEADING_LINE_RE.match(stripped)
        if h:
            text = h.group(1).strip()
            if text:
                return text[:TITLE_MAX_CHARS]
        plain = _INLINE_MD_RE.sub("", stripped)
        plain = _LINK_RE.sub(r"\1", plain)
        plain = re.sub(r"\s+", " ", plain).strip()
        if plain:
            return plain[:TITLE_MAX_CHARS]
    return fallback[:TITLE_MAX_CHARS]


def _derive_preview(body: str) -> str:
    """Flatten the first ``PREVIEW_CHARS`` of prose for keyword search."""
    return _flatten_body(body)[:PREVIEW_CHARS]


def _derive_classify_text(body: str) -> str:
    """Wider window used only by the topic classifier. Bigger than the
    preview because topic keywords routinely sit past the opening
    paragraph in ChatGPT conversations."""
    return _flatten_body(body)[:CLASSIFY_CHARS]


def _flatten_body(body: str) -> str:
    """Strip frontmatter + markdown formatting and collapse whitespace.
    Shared between preview derivation and the classifier window so both
    see the same cleaned prose.
    """
    clean = _FRONTMATTER_RE.sub("", body or "", count=1)
    clean = _INLINE_MD_RE.sub("", clean)
    clean = _LINK_RE.sub(r"\1", clean)
    return re.sub(r"\s+", " ", clean).strip()


def _entry_for_file(vault_root: Path, abs_path: Path) -> IndexEntry | None:
    """Read ``abs_path`` and turn it into an :class:`IndexEntry`.

    Returns ``None`` if the file vanished or is unreadable — caller
    skips it rather than aborting the whole scan.
    """
    try:
        raw_bytes = abs_path.read_bytes()
    except OSError:
        return None
    body = raw_bytes.decode("utf-8", errors="replace")
    try:
        st = abs_path.stat()
        mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat()
        size = st.st_size
    except OSError:
        mtime = ""
        size = len(raw_bytes)
    try:
        rel = abs_path.relative_to(vault_root).as_posix()
    except ValueError:
        rel = abs_path.name
    stem = abs_path.stem
    title = _derive_title(body, fallback=stem)
    preview = _derive_preview(body)
    # Classify off a wider window than the preview. Most ChatGPT
    # threads open with context/pleasantries before the operator
    # says anything topical, so classifying on title+preview alone
    # dropped the vast majority of threads into "general". Using
    # the first CLASSIFY_CHARS of the cleaned body catches the
    # actual substance.
    topic = classify_topic(f"{title}\n{_derive_classify_text(body)}")
    return IndexEntry(
        path=rel, title=title, preview=preview,
        topic=topic, mtime=mtime, size=size,
    )


# ── build / load / query ─────────────────────────────────────────────


def build_index(vault_root: Path | str) -> int:
    """Walk ``ingested/chatgpt/*.md`` and rewrite the JSONL index.

    Returns the number of entries written. Safe to run concurrently
    with vault writes — we stream entries into a sibling tempfile
    and atomically rename over the live index file at the end.

    The index file itself (``_index.jsonl``) is excluded from the scan
    so we don't bootstrap an entry for ourselves.
    """
    root = Path(vault_root).expanduser()
    chatgpt_dir = root / CHATGPT_DIR
    index_path = root / INDEX_FILE
    if not chatgpt_dir.is_dir():
        log.info("chatgpt_index_skip_missing_dir", dir=str(chatgpt_dir))
        # Still write an empty index so downstream loaders don't have
        # to special-case "no file" vs "no entries".
        index_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_lines(index_path, [])
        return 0

    entries: list[IndexEntry] = []
    for abs_path in sorted(chatgpt_dir.rglob("*.md")):
        if abs_path.name.startswith("_"):
            # Index sidecar or any other underscore-prefixed convention
            # file; skip.
            continue
        entry = _entry_for_file(root, abs_path)
        if entry is not None:
            entries.append(entry)

    index_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_lines(
        index_path, [e.to_json_line() for e in entries],
    )
    log.info(
        "chatgpt_index_built",
        total=len(entries),
        path=str(index_path.relative_to(root)) if index_path.is_relative_to(root) else str(index_path),
    )
    return len(entries)


def load_index(vault_root: Path | str) -> list[IndexEntry]:
    """Read every entry from the JSONL index on disk.

    Silently returns ``[]`` if the index file is missing — callers
    treat "no index" and "empty index" identically so startup-before-
    first-build doesn't produce an error.
    """
    root = Path(vault_root).expanduser()
    index_path = root / INDEX_FILE
    if not index_path.exists():
        return []
    out: list[IndexEntry] = []
    try:
        with index_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                entry = IndexEntry.from_json_line(line)
                if entry is not None:
                    out.append(entry)
    except OSError as e:
        log.warning("chatgpt_index_load_failed", error=str(e))
        return []
    return out


@dataclass(frozen=True)
class QueryHit:
    entry: IndexEntry
    score: int


def query_chatgpt_vault(
    vault_root: Path | str,
    query: str,
    *,
    top_k: int = 5,
    topic: str | None = None,
) -> list[QueryHit]:
    """Return the top ``top_k`` entries for ``query``.

    Scoring: case-insensitive word tokens from the query. Each token
    scores 3 points per occurrence in the title, 1 per occurrence in
    the preview. Ties break on newest ``mtime``. Optional ``topic``
    narrows the result set to a single topic bucket.

    Empty / whitespace-only queries return ``[]`` rather than dumping
    the whole index on the caller — callers that want the full list
    should use :func:`load_index` directly.
    """
    tokens = [t for t in re.split(r"\s+", (query or "").lower()) if len(t) >= 2]
    if not tokens:
        return []
    entries = load_index(vault_root)
    if topic:
        entries = [e for e in entries if e.topic == topic]
    scored: list[QueryHit] = []
    for e in entries:
        t_lower = e.title.lower()
        p_lower = e.preview.lower()
        score = 0
        for tok in tokens:
            if not tok:
                continue
            score += t_lower.count(tok) * 3
            score += p_lower.count(tok)
        if score > 0:
            scored.append(QueryHit(entry=e, score=score))
    scored.sort(key=lambda h: (h.score, h.entry.mtime), reverse=True)
    return scored[:top_k]


# ── helpers ──────────────────────────────────────────────────────────


def _atomic_write_lines(dest: Path, lines: list[str]) -> None:
    """Atomic JSONL write: tempfile in the same dir, then os.replace."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=dest.stem + ".", suffix=".tmp", dir=dest.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line)
                fh.write("\n")
        os.replace(tmp_path, dest)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


# ── scheduler ────────────────────────────────────────────────────────
#
# A background task that rebuilds the index once on startup (so a
# just-ingested export is queryable before the first nightly run) and
# then at 03:00 local every day. Kept in this module so the index
# lifecycle lives in one place; wired into app startup from
# core/api/app.py.

#: Local time-of-day for the nightly rebuild. Matches the spec ("3 AM")
#: without introducing a hard dep on a cron library — we just sleep
#: until the next local 03:00.
NIGHTLY_REBUILD_AT = time(hour=3, minute=0)


def _seconds_until_next(now: datetime, target: time) -> float:
    """Wall-clock seconds from ``now`` until the next local ``target``
    time-of-day. Always returns a positive value so a tick exactly at
    ``target`` schedules the following day, not the current one.
    """
    today_target = now.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0,
    )
    if today_target <= now:
        today_target = today_target + timedelta(days=1)
    return (today_target - now).total_seconds()


async def _run_scheduler(vault_root: Path) -> None:
    """Index-on-boot + nightly rebuild loop. Silently swallows every
    exception so a transient filesystem blip doesn't kill the task."""
    try:
        build_index(vault_root)
    except Exception as e:
        log.exception("chatgpt_index_boot_build_failed", error=str(e))
    while True:
        try:
            delay = _seconds_until_next(
                datetime.now().astimezone(),
                NIGHTLY_REBUILD_AT,
            )
            await asyncio.sleep(delay)
            build_index(vault_root)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("chatgpt_index_nightly_build_failed", error=str(e))
            # Guarantee forward progress — don't busy-loop on repeated
            # failures; wait an hour and try again.
            await asyncio.sleep(3600)


def spawn_scheduler(vault_root: Path | str) -> asyncio.Task[None]:
    """Create the boot + nightly rebuild task. Caller holds the
    reference so the event loop doesn't garbage-collect it."""
    root = Path(vault_root).expanduser()
    return asyncio.create_task(
        _run_scheduler(root),
        name="chatgpt-index-scheduler",
    )


__all__ = [
    "CHATGPT_DIR",
    "INDEX_FILE",
    "NIGHTLY_REBUILD_AT",
    "TOPIC_LABELS",
    "IndexEntry",
    "QueryHit",
    "build_index",
    "classify_topic",
    "load_index",
    "query_chatgpt_vault",
    "spawn_scheduler",
]
