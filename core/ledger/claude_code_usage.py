"""Scan Claude Code CLI session logs to count subscription-billed turns.

Claude Code writes one JSONL file per session under
``~/.claude/projects/<project-slug>/<uuid>.jsonl``. The Max
subscription cap is "messages per 5 hours" where a *message* is one
operator-initiated prompt — NOT one assistant API call. Internally a
single user prompt can produce many ``type=assistant`` lines (one
per tool-use round-trip), so counting assistant entries over-counts
by 5-20x in tool-heavy workflows and makes the ring wildly wrong.

What we actually count:

* ``count`` — ``type=user`` entries whose ``message.content`` is NOT
  a ``tool_result``. That is: prompts the operator typed (or the
  Pilk bridge forwarded). Maps 1:1 to what Anthropic bills against
  the Max bucket. Tool-result feeds are skipped because Anthropic
  doesn't count them as separate messages.
* ``input_tokens`` / ``output_tokens`` / cache counters — summed
  from ``type=assistant`` entries (the rows that carry usage blobs).
  These are diagnostic: they let the dashboard show how much token
  work a session generated even when it was cheap on the message
  counter.

The scan is cheap: one ``mtime`` filter drops every session file that
couldn't possibly contain entries in the window, then we stream the
survivors line-by-line. Directory missing or unreadable → return 0,
never raise. Failures on individual lines are swallowed (session
files can be truncated mid-write by a crash).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.logging import get_logger

log = get_logger("pilkd.ledger.claude_code_usage")

#: Standard Claude Code state dir. Override with ``CLAUDE_CODE_HOME``
#: for sandboxed test runs.
CLAUDE_CODE_PROJECTS_DIR = "projects"


def _default_home() -> Path:
    return Path(os.environ.get("CLAUDE_CODE_HOME", str(Path.home() / ".claude")))


@dataclass(frozen=True)
class ClaudeCodeUsage:
    """Result of one scan. ``count`` is assistant turns (= billable
    API calls) within the requested window."""

    count: int
    window_hours: int
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    sessions_sampled: int
    oldest_at: str | None

    def to_public(self) -> dict:
        return {
            "count": self.count,
            "window_hours": self.window_hours,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "sessions_sampled": self.sessions_sampled,
            "oldest_at": self.oldest_at,
        }


def scan_usage(
    *,
    window_hours: int = 5,
    home: Path | None = None,
) -> ClaudeCodeUsage:
    """Count Claude Code CLI assistant turns in the last ``window_hours``.

    Returns a zero-count ``ClaudeCodeUsage`` when ``~/.claude/projects``
    doesn't exist (Claude Code not installed) or the directory is
    unreadable for any reason.
    """
    root = (home or _default_home()) / CLAUDE_CODE_PROJECTS_DIR
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=window_hours)
    # mtime pre-filter: drop files that haven't been touched since well
    # before the window opened. Small buffer handles session files
    # opened slightly before the window and still being written now.
    mtime_cutoff = (now - timedelta(hours=window_hours + 1)).timestamp()

    if not root.is_dir():
        return ClaudeCodeUsage(
            count=0, window_hours=window_hours,
            input_tokens=0, output_tokens=0,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
            sessions_sampled=0, oldest_at=None,
        )

    total = 0
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_write = 0
    oldest: datetime | None = None
    sessions_sampled = 0
    try:
        iterator = root.rglob("*.jsonl")
    except OSError as e:
        log.warning("claude_code_usage_scan_failed", detail=str(e))
        return ClaudeCodeUsage(
            count=0, window_hours=window_hours,
            input_tokens=0, output_tokens=0,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
            sessions_sampled=0, oldest_at=None,
        )

    for path in iterator:
        try:
            if path.stat().st_mtime < mtime_cutoff:
                continue
        except OSError:
            continue
        sessions_sampled += 1
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    entry = _parse_line(line)
                    if entry is None:
                        continue
                    kind, ts, usage = entry
                    if ts < cutoff:
                        continue
                    if kind == "user_prompt":
                        total += 1
                        if oldest is None or ts < oldest:
                            oldest = ts
                    elif kind == "assistant":
                        input_tokens += int(usage.get("input_tokens") or 0)
                        output_tokens += int(usage.get("output_tokens") or 0)
                        cache_read += int(usage.get("cache_read_input_tokens") or 0)
                        cache_write += int(
                            usage.get("cache_creation_input_tokens") or 0,
                        )
        except OSError:
            continue

    return ClaudeCodeUsage(
        count=total,
        window_hours=window_hours,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
        sessions_sampled=sessions_sampled,
        oldest_at=oldest.isoformat() if oldest else None,
    )


def _parse_line(line: str) -> tuple[str, datetime, dict] | None:
    """Classify one JSONL line into (kind, timestamp, usage).

    ``kind`` is one of:

    * ``"user_prompt"`` — a ``type=user`` entry whose content is NOT
      a tool_result. Counts toward the Max message tally.
    * ``"assistant"`` — a ``type=assistant`` entry carrying a usage
      blob. Contributes tokens; does NOT tick the message counter.

    Returns ``None`` for tool_result feeds, system messages,
    queue-operation markers, and anything malformed.
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    kind = obj.get("type")
    if kind not in ("user", "assistant"):
        return None

    ts_raw = obj.get("timestamp")
    if not isinstance(ts_raw, str):
        return None
    try:
        # Claude Code writes ``2026-04-23T20:42:19.123Z`` — the Z
        # suffix is valid ISO-8601 but Python's ``fromisoformat``
        # didn't accept it until 3.11. Normalize to +00:00.
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)

    message = obj.get("message")
    if not isinstance(message, dict):
        return None

    if kind == "user":
        # Skip tool_result continuations — those are auto-generated
        # turn-fillers, not operator-typed prompts.
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(c, dict) and c.get("type") == "tool_result"
            for c in content
        ):
            return None
        return ("user_prompt", ts, {})

    # assistant — usage is optional but usually present
    usage = message.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    return ("assistant", ts, usage)


__all__ = ["ClaudeCodeUsage", "scan_usage"]
