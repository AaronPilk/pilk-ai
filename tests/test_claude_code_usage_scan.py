"""Scan of Claude Code CLI session logs for subscription-usage counts.

Core guarantees:
  * Only ``type=assistant`` lines with a ``message.usage`` block count.
  * Entries outside the ``window_hours`` slider are excluded.
  * Missing ``~/.claude/projects`` directory returns a zero-count
    result rather than raising (important: not every PILK user has
    Claude Code installed).
  * Truncated / malformed JSONL lines are skipped, not fatal.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.ledger.claude_code_usage import scan_usage


def _assistant_entry(
    *, timestamp: datetime, model: str = "claude-opus-4-7",
    usage: dict | None = None,
) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "message": {
            "model": model,
            "usage": usage or {
                "input_tokens": 10,
                "output_tokens": 50,
                "cache_read_input_tokens": 2000,
                "cache_creation_input_tokens": 0,
            },
        },
    })


def _user_entry(*, timestamp: datetime, text: str = "hi") -> str:
    return json.dumps({
        "type": "user",
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "message": {"role": "user", "content": text},
    })


@pytest.fixture
def claude_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / ".claude"
    (home / "projects" / "-home-user-pilk-ai").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CODE_HOME", str(home))
    return home


def _session(home: Path, name: str, lines: list[str]) -> Path:
    path = home / "projects" / "-home-user-pilk-ai" / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_scan_counts_assistant_entries_in_window(claude_home: Path) -> None:
    now = datetime.now(UTC)
    _session(claude_home, "sess-1.jsonl", [
        _assistant_entry(timestamp=now - timedelta(minutes=5)),
        _assistant_entry(timestamp=now - timedelta(minutes=30)),
        _assistant_entry(timestamp=now - timedelta(hours=2)),
        _user_entry(timestamp=now - timedelta(minutes=4)),
        _assistant_entry(timestamp=now - timedelta(hours=7)),
    ])
    result = scan_usage(window_hours=5, home=claude_home)
    assert result.count == 3
    assert result.sessions_sampled == 1


def test_scan_sums_token_counts(claude_home: Path) -> None:
    now = datetime.now(UTC)
    _session(claude_home, "sess.jsonl", [
        _assistant_entry(
            timestamp=now,
            usage={
                "input_tokens": 10,
                "output_tokens": 100,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 200,
            },
        ),
        _assistant_entry(
            timestamp=now - timedelta(minutes=5),
            usage={
                "input_tokens": 20,
                "output_tokens": 200,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 100,
            },
        ),
    ])
    result = scan_usage(window_hours=5, home=claude_home)
    assert result.input_tokens == 30
    assert result.output_tokens == 300
    assert result.cache_read_input_tokens == 500
    assert result.cache_creation_input_tokens == 300


def test_scan_walks_multiple_sessions(claude_home: Path) -> None:
    now = datetime.now(UTC)
    _session(claude_home, "a.jsonl", [
        _assistant_entry(timestamp=now - timedelta(minutes=1)),
    ])
    _session(claude_home, "b.jsonl", [
        _assistant_entry(timestamp=now - timedelta(minutes=2)),
        _assistant_entry(timestamp=now - timedelta(minutes=3)),
    ])
    result = scan_usage(window_hours=5, home=claude_home)
    assert result.count == 3
    assert result.sessions_sampled == 2


def test_scan_missing_home_dir_returns_zero(tmp_path: Path) -> None:
    result = scan_usage(window_hours=5, home=tmp_path / "does-not-exist")
    assert result.count == 0
    assert result.sessions_sampled == 0
    assert result.oldest_at is None


def test_scan_skips_files_outside_window(claude_home: Path) -> None:
    now = datetime.now(UTC)
    old = _session(claude_home, "old.jsonl", [
        _assistant_entry(timestamp=now - timedelta(minutes=1)),
    ])
    old_ts = (now - timedelta(hours=24)).timestamp()
    os.utime(old, (old_ts, old_ts))

    _session(claude_home, "fresh.jsonl", [
        _assistant_entry(timestamp=now - timedelta(minutes=1)),
    ])

    result = scan_usage(window_hours=5, home=claude_home)
    assert result.count == 1
    assert result.sessions_sampled == 1


def test_scan_tolerates_malformed_lines(claude_home: Path) -> None:
    now = datetime.now(UTC)
    _session(claude_home, "mixed.jsonl", [
        _assistant_entry(timestamp=now - timedelta(minutes=1)),
        "{not-json",
        '{"type":"assistant"}',
        "",
        _assistant_entry(timestamp=now - timedelta(minutes=2)),
    ])
    result = scan_usage(window_hours=5, home=claude_home)
    assert result.count == 2
