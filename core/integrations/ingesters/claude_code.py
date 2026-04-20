"""Claude Code transcript ingester.

Claude Code persists every session under ``~/.claude/projects/<slug>/``
as newline-delimited JSON — one message per line. ``<slug>`` is a
munged form of the project's filesystem path (forward slashes →
dashes; leading `-`).

We ingest at **project granularity**, not session: for each project
slug we aggregate every session into one dense markdown note so the
brain vault can show "everything we ever chatted about pilk-ai" as a
single graph node. Individual sessions are still represented as
second-level headings within the note, ordered newest-first.

Expected JSONL line shapes (from Claude Code, current as of Claude 4):

    {"type": "user", "message": {"content": "text" | [{...}]}, ...}
    {"type": "assistant", "message": {"content": [{"type": "text",
        "text": "…"}]}, ...}
    {"type": "system", ...}     — skipped (session metadata)

We're tolerant of schema drift — missing keys become empty, unknown
types are skipped rather than crashing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.integrations.ingesters import IngestedNote
from core.logging import get_logger

log = get_logger("pilkd.ingest.claude_code")

DEFAULT_ROOT = Path.home() / ".claude" / "projects"
MAX_TURNS_PER_SESSION = 200      # keep notes readable
MAX_TURN_CHARS = 2400            # truncate very long turns with an ellipsis


@dataclass(frozen=True)
class ClaudeCodeProject:
    """One project's worth of aggregated Claude Code activity."""

    slug: str
    original_path: str  # best-effort reconstruction of the real path
    sessions: list[ClaudeCodeSession]

    @property
    def title(self) -> str:
        # The slug stem is usually enough — most slugs encode the
        # last segment of the project path.
        segments = [s for s in self.original_path.split("/") if s]
        return segments[-1] if segments else self.slug


@dataclass(frozen=True)
class ClaudeCodeSession:
    session_id: str
    started_at: datetime | None
    turns: list[ClaudeCodeTurn]

    @property
    def label(self) -> str:
        if self.started_at:
            return self.started_at.strftime("%Y-%m-%d %H:%M")
        return self.session_id


@dataclass(frozen=True)
class ClaudeCodeTurn:
    role: str  # "user" | "assistant"
    text: str
    at: datetime | None


def _slug_to_path(slug: str) -> str:
    """Best-effort reverse of the Claude Code slug convention
    (``-Users-aaron-pilk-ai`` → ``/Users/aaron/pilk-ai``). Slug
    munging is lossy — directory names containing dashes become
    ambiguous — but it's close enough for a note title."""
    return "/" + slug.lstrip("-").replace("-", "/")


def _coerce_text(raw: object) -> str:
    """Flatten Claude Code's ``message.content`` into a string.

    Tolerant of three shapes:
    - raw string
    - list of content-block dicts with ``type`` + ``text`` (or
      ``content``) — we pick the text blocks and ignore tool calls /
      thinking blocks
    - unexpected shape → empty string (surfaced as "(no text)")
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        chunks: list[str] = []
        for block in raw:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind in {"text", "output_text"}:
                text = block.get("text") or block.get("content") or ""
                if isinstance(text, str):
                    chunks.append(text)
            elif kind == "tool_use":
                # Include tool names but not arguments — the blob
                # would swamp the note and isn't useful for later
                # recall.
                tool = block.get("name") or "tool"
                chunks.append(f"_[tool: {tool}]_")
            elif kind == "tool_result":
                # Same reasoning as above.
                chunks.append("_[tool result]_")
        return "\n".join(chunks)
    return ""


def _iso_to_dt(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        s = str(raw)
        # Python 3.11 handles Z suffix natively; older
        # implementations don't. Normalise.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _parse_session(path: Path) -> ClaudeCodeSession | None:
    """Parse one .jsonl session file. Skips blank lines + malformed
    rows silently; a single corrupt row never kills the session."""
    turns: list[ClaudeCodeTurn] = []
    started_at: datetime | None = None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("claude_session_read_failed", path=str(path), error=str(e))
        return None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind not in {"user", "assistant"}:
            continue
        msg = obj.get("message") or {}
        if not isinstance(msg, dict):
            continue
        text = _coerce_text(msg.get("content")) or ""
        text = text.strip()
        if not text:
            continue
        when = _iso_to_dt(obj.get("timestamp")) or _iso_to_dt(
            msg.get("timestamp"),
        )
        if started_at is None and when is not None:
            started_at = when
        # Respect a hard turn cap so a runaway session doesn't
        # produce a 50MB note.
        if len(turns) >= MAX_TURNS_PER_SESSION:
            continue
        if len(text) > MAX_TURN_CHARS:
            text = text[:MAX_TURN_CHARS] + "\n\n… [truncated]"
        turns.append(
            ClaudeCodeTurn(role=kind, text=text, at=when),
        )
    if not turns:
        return None
    return ClaudeCodeSession(
        session_id=path.stem,
        started_at=started_at,
        turns=turns,
    )


def scan_projects(root: Path = DEFAULT_ROOT) -> list[ClaudeCodeProject]:
    """Walk ~/.claude/projects/ and return one aggregate per slug.
    Sorted with the most recently active project first so the first
    N-limited ingest surfaces the freshest content."""
    if not root.is_dir():
        return []
    projects: list[ClaudeCodeProject] = []
    for slug_dir in sorted(root.iterdir()):
        if not slug_dir.is_dir():
            continue
        sessions: list[ClaudeCodeSession] = []
        for f in slug_dir.glob("*.jsonl"):
            parsed = _parse_session(f)
            if parsed is not None:
                sessions.append(parsed)
        if not sessions:
            continue
        sessions.sort(
            key=lambda s: s.started_at or datetime.fromtimestamp(0, tz=UTC),
            reverse=True,
        )
        projects.append(
            ClaudeCodeProject(
                slug=slug_dir.name,
                original_path=_slug_to_path(slug_dir.name),
                sessions=sessions,
            )
        )
    # Projects ordered by their most recent session.
    projects.sort(
        key=lambda p: (
            p.sessions[0].started_at
            if p.sessions and p.sessions[0].started_at
            else datetime.fromtimestamp(0, tz=UTC)
        ),
        reverse=True,
    )
    return projects


_SAFE_STEM = re.compile(r"[^a-z0-9 _\-().']+")


def _safe_stem(s: str) -> str:
    """Produce a vault-safe filename stem from a project title."""
    low = s.lower().replace("/", "-")
    low = _SAFE_STEM.sub("-", low)
    return low.strip("-") or "project"


def render_project_note(project: ClaudeCodeProject) -> IngestedNote:
    """Assemble one markdown note for a project. Structure:

    ```
    # <title>

    _Claude Code project · <original_path>_

    Last activity: <date>
    Sessions: <count>

    ## <session label>

    ### User
    …

    ### Assistant
    …
    ```
    """
    last = project.sessions[0].started_at
    header = [
        f"# {project.title}",
        "",
        f"_Claude Code project · `{project.original_path}`_",
        "",
        f"- Last activity: {last.isoformat() if last else 'unknown'}",
        f"- Sessions: {len(project.sessions)}",
        "",
    ]
    body_chunks: list[str] = []
    for s in project.sessions:
        body_chunks.append(f"## Session — {s.label}")
        body_chunks.append("")
        for t in s.turns:
            role_title = "User" if t.role == "user" else "Assistant"
            when = f" _({t.at.isoformat()})_" if t.at else ""
            body_chunks.append(f"### {role_title}{when}")
            body_chunks.append("")
            body_chunks.append(t.text)
            body_chunks.append("")
        body_chunks.append("")  # blank between sessions
    stem = _safe_stem(project.title)
    return IngestedNote(
        path=f"ingested/claude-code/{stem}.md",
        body="\n".join(header + body_chunks),
        source_id=project.slug,
        title=project.title,
    )


__all__ = [
    "DEFAULT_ROOT",
    "ClaudeCodeProject",
    "ClaudeCodeSession",
    "ClaudeCodeTurn",
    "render_project_note",
    "scan_projects",
]
