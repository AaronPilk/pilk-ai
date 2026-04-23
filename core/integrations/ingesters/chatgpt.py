"""ChatGPT export ingester.

ChatGPT's "Export data" zip contains (among other things) a
``conversations.json`` with an array of conversation objects. Each
has:

- ``id`` / ``conversation_id`` — stable identifier
- ``title`` — operator-visible title
- ``create_time`` / ``update_time`` — floats (unix seconds)
- ``mapping`` — dict of node-id → {id, parent, children[], message{}}

The message thread is stored as a *tree* (every message has a
parent) because ChatGPT supports forked conversations. We flatten by
walking the current path — start from the root message, then at
each step pick the newest child. That gives the linear thread the
operator actually saw; forked branches are dropped (they're almost
always abandoned retries).

System / tool / empty messages are skipped. Only user + assistant
text lands in the note.

Tolerant: input can be a path to a .zip (we extract conversations.
json from it) or directly to conversations.json.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.integrations.ingesters import IngestedNote
from core.logging import get_logger

log = get_logger("pilkd.ingest.chatgpt")

MAX_TURNS_PER_CONVERSATION = 300
MAX_TURN_CHARS = 2400

# ChatGPT's export used to land a single ``conversations.json`` at the
# root of the zip. Large accounts now get a sharded layout —
# ``conversations-000.json``, ``conversations-001.json``, … — each
# holding a slice of the full array. Match both. The basename is
# checked (not the full archive path) so a nested layout like
# ``export/conversations-000.json`` still works.
_CONVERSATIONS_JSON_RE = re.compile(r"^conversations(?:-\d+)?\.json$")


@dataclass(frozen=True)
class ChatGPTTurn:
    role: str  # "user" | "assistant"
    text: str
    at: datetime | None


@dataclass(frozen=True)
class ChatGPTConversation:
    conversation_id: str
    title: str
    created_at: datetime | None
    updated_at: datetime | None
    turns: list[ChatGPTTurn]


class ChatGPTIngestError(Exception):
    pass


def _parse_conversations_bytes(src: Path, member: str, raw: bytes) -> list[dict]:
    """Parse one ``conversations.json`` blob into a list of raw
    conversation dicts. Shared by the zip + raw-json loaders so error
    messages all point at the same failure mode."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ChatGPTIngestError(f"bad JSON in {src} ({member}): {e}") from e
    if not isinstance(data, list):
        raise ChatGPTIngestError(
            f"{src} ({member}): conversations.json root is not a list"
        )
    return data


def _load_conversations_json(src: Path) -> list[dict]:
    """Accept a path to a .zip (from ChatGPT data-export) or directly
    to a conversations.json file. Return the merged conversation list.

    Handles the current sharded export format — ChatGPT now splits
    very large accounts' conversations across ``conversations-000.
    json``, ``conversations-001.json``, … instead of a single
    ``conversations.json``. We match both layouts and concatenate the
    shards in numeric order so the final list mirrors what a single-
    file export would have produced.
    """
    if not src.exists():
        raise ChatGPTIngestError(f"not found: {src}")
    if src.is_file() and src.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(src) as zf:
                members = [
                    name for name in zf.namelist()
                    if _CONVERSATIONS_JSON_RE.fullmatch(Path(name).name or "")
                ]
                if not members:
                    # Surface a short sample of the zip contents so the
                    # next format change is obvious rather than opaque.
                    sample = ", ".join(sorted(zf.namelist())[:6]) or "(empty)"
                    raise ChatGPTIngestError(
                        f"{src}: zip contains no conversations.json "
                        f"(saw: {sample})"
                    )
                # Sort by the numeric suffix so shards merge in order —
                # ``conversations.json`` (unsharded) sorts to 0 and ends
                # up first when both layouts somehow coexist.
                members.sort(key=_shard_sort_key)
                data: list[dict] = []
                for member in members:
                    with zf.open(member) as fh:
                        raw = fh.read()
                    data.extend(_parse_conversations_bytes(src, member, raw))
                if len(members) > 1:
                    log.info(
                        "chatgpt_export_merged_shards",
                        source=str(src),
                        shards=len(members),
                        conversations=len(data),
                    )
                return data
        except zipfile.BadZipFile as e:
            raise ChatGPTIngestError(f"bad zip: {src}: {e}") from e
    if src.is_file() and src.suffix.lower() == ".json":
        return _parse_conversations_bytes(src, src.name, src.read_bytes())
    raise ChatGPTIngestError(
        f"{src}: expected a .zip or conversations.json, got {src.suffix}"
    )


def _shard_sort_key(name: str) -> tuple[int, str]:
    """Numeric-aware order for ``conversations-NNN.json`` shards.
    ``conversations.json`` (no number) sorts before any numbered shard.
    """
    base = Path(name).name
    m = re.fullmatch(r"conversations(?:-(\d+))?\.json", base)
    if m and m.group(1) is not None:
        return (int(m.group(1)), name)
    return (-1, name)


def _ts_to_dt(raw: object) -> datetime | None:
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=UTC)
    except (TypeError, ValueError):
        return None


def _message_text(msg: dict) -> str:
    """Flatten a ChatGPT message's content parts to plain text."""
    content = msg.get("content") or {}
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts") or []
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for p in parts:
        if isinstance(p, str):
            chunks.append(p)
        elif isinstance(p, dict):
            # Some parts are dicts with {text: "..."} — extract that.
            text = p.get("text") or ""
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(c for c in chunks if c)


def _walk_thread(mapping: dict) -> list[dict]:
    """Flatten the ChatGPT message tree to a linear thread.

    Strategy: find the root (parent=None), then at each step pick
    the child with the latest ``create_time`` — that's the branch
    the operator was actually on. ChatGPT's UI does the same thing.
    """
    if not isinstance(mapping, dict) or not mapping:
        return []
    # Find root: a node whose parent is None (or missing from mapping).
    root_id = None
    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            continue
        parent = node.get("parent")
        if parent is None or parent not in mapping:
            root_id = node_id
            break
    if root_id is None:
        # Fall back to any node — just pick a stable one.
        root_id = next(iter(mapping))
    thread: list[dict] = []
    current = root_id
    visited: set[str] = set()
    while current and current not in visited:
        visited.add(current)
        node = mapping.get(current)
        if not isinstance(node, dict):
            break
        msg = node.get("message")
        if isinstance(msg, dict):
            thread.append(msg)
        children = node.get("children") or []
        if not children:
            break
        # Pick the child with the latest create_time; missing
        # timestamps fall to the back.
        def _child_time(cid: str) -> float:
            cn = mapping.get(cid) or {}
            cm = cn.get("message") or {}
            return float(cm.get("create_time") or 0.0)

        current = max(children, key=_child_time)
    return thread


def _parse_conversation(raw: dict) -> ChatGPTConversation | None:
    cid = str(raw.get("id") or raw.get("conversation_id") or "").strip()
    if not cid:
        return None
    title = str(raw.get("title") or "").strip() or "Untitled conversation"
    created_at = _ts_to_dt(raw.get("create_time"))
    updated_at = _ts_to_dt(raw.get("update_time")) or created_at
    messages = _walk_thread(raw.get("mapping") or {})
    turns: list[ChatGPTTurn] = []
    for msg in messages:
        author = (msg.get("author") or {}).get("role") or msg.get("role")
        if author not in {"user", "assistant"}:
            continue
        text = _message_text(msg).strip()
        if not text:
            continue
        if len(turns) >= MAX_TURNS_PER_CONVERSATION:
            break
        if len(text) > MAX_TURN_CHARS:
            text = text[:MAX_TURN_CHARS] + "\n\n… [truncated]"
        turns.append(
            ChatGPTTurn(
                role=author,
                text=text,
                at=_ts_to_dt(msg.get("create_time")),
            )
        )
    if not turns:
        return None
    return ChatGPTConversation(
        conversation_id=cid,
        title=title,
        created_at=created_at,
        updated_at=updated_at,
        turns=turns,
    )


def parse_export(src: Path) -> list[ChatGPTConversation]:
    """Parse a ChatGPT export zip or raw conversations.json → list
    of conversations, newest-first. Corrupt individual conversations
    are skipped silently."""
    raw_list = _load_conversations_json(src)
    out: list[ChatGPTConversation] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        parsed = _parse_conversation(raw)
        if parsed is not None:
            out.append(parsed)
    out.sort(
        key=lambda c: (
            c.updated_at
            or c.created_at
            or datetime.fromtimestamp(0, tz=UTC)
        ),
        reverse=True,
    )
    return out


_SAFE_STEM = re.compile(r"[^a-z0-9 _\-().']+")


def _safe_stem(s: str, fallback: str) -> str:
    low = s.lower().replace("/", "-")
    low = _SAFE_STEM.sub("-", low)
    trimmed = low.strip("-")
    if not trimmed:
        return fallback
    # Cap at 60 so we don't hit path-length limits with long titles.
    return trimmed[:60]


def render_conversation_note(conv: ChatGPTConversation) -> IngestedNote:
    """One markdown note per conversation, stored under
    ``ingested/chatgpt/<YYYY-MM-DD>-<slug>.md``. Dating the filename
    lets the operator scan by date in Obsidian's file browser."""
    date_prefix = (
        conv.updated_at or conv.created_at or datetime.fromtimestamp(0, tz=UTC)
    ).strftime("%Y-%m-%d")
    stem = _safe_stem(conv.title, fallback="conversation")
    header = [
        f"# {conv.title}",
        "",
        f"_ChatGPT conversation · id `{conv.conversation_id}`_",
        "",
        f"- Created: {conv.created_at.isoformat() if conv.created_at else 'unknown'}",
        f"- Last update: {conv.updated_at.isoformat() if conv.updated_at else 'unknown'}",
        f"- Turns: {len(conv.turns)}",
        "",
    ]
    chunks: list[str] = []
    for t in conv.turns:
        role_title = "User" if t.role == "user" else "Assistant"
        when = f" _({t.at.isoformat()})_" if t.at else ""
        chunks.append(f"### {role_title}{when}")
        chunks.append("")
        chunks.append(t.text)
        chunks.append("")
    return IngestedNote(
        path=f"ingested/chatgpt/{date_prefix}-{stem}.md",
        body="\n".join(header + chunks),
        source_id=conv.conversation_id,
        title=conv.title,
    )


__all__ = [
    "ChatGPTConversation",
    "ChatGPTIngestError",
    "ChatGPTTurn",
    "parse_export",
    "render_conversation_note",
]
