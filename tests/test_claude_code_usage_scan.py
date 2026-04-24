"""Scan of Claude Code CLI session logs for subscription-usage counts.

Core guarantees:
  * Count ``type=user`` entries whose content is NOT a tool_result —
    those are operator-typed prompts, which is what Anthropic bills
    against the Max 5-hour message cap.
  * Tool-result continuations (``type=user`` with structured
    ``tool_result`` content) don't count — they're auto-generated
    agentic turn-fillers.
  * Token totals come from ``type=assistant`` rows (usage blobs live
    there) — diagnostic only, they don't tick the message counter.
  * Entries outside the ``window_hours`` slider are excluded.
  * Missing ``~/.claude/projects`` directory returns a zero-count
    result rather than raising.
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


def _tool_result_user_entry(*, timestamp: datetime) -> str:
    """User row whose content is a tool_result — these are auto-
    generated feedback rounds, not operator-typed prompts, and must
    NOT count toward the subscription cap."""
    return json.dumps({
        "type": "user",
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "abc",
                    "content": "ok",
                }
            ],
        },
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


def test_scan_counts_operator_prompts_in_window(claude_home: Path) -> None:
    now = datetime.now(UTC)
    _session(claude_home, "sess-1.jsonl", [
        # Three real user prompts inside the window.
        _user_entry(timestamp=now - timedelta(minutes=5)),
        _user_entry(timestamp=now - timedelta(minutes=30)),
        _user_entry(timestamp=now - timedelta(hours=2)),
        # Tool-result feed — NOT a real prompt, skipped.
        _tool_result_user_entry(timestamp=now - timedelta(minutes=10)),
        # Assistant rounds don't tick the counter.
        _assistant_entry(timestamp=now - timedelta(minutes=5)),
        _assistant_entry(timestamp=now - timedelta(minutes=30)),
        # Outside the 5h window — skipped.
        _user_entry(timestamp=now - timedelta(hours=7)),
    ])
    result = scan_usage(window_hours=5, home=claude_home)
    assert result.count == 3
    assert result.sessions_sampled == 1


def test_scan_sums_token_counts_from_assistant(claude_home: Path) -> None:
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
        _user_entry(timestamp=now - timedelta(minutes=1)),
    ])
    _session(claude_home, "b.jsonl", [
        _user_entry(timestamp=now - timedelta(minutes=2)),
        _user_entry(timestamp=now - timedelta(minutes=3)),
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
        _user_entry(timestamp=now - timedelta(minutes=1)),
    ])
    old_ts = (now - timedelta(hours=24)).timestamp()
    os.utime(old, (old_ts, old_ts))

    _session(claude_home, "fresh.jsonl", [
        _user_entry(timestamp=now - timedelta(minutes=1)),
    ])

    result = scan_usage(window_hours=5, home=claude_home)
    assert result.count == 1
    assert result.sessions_sampled == 1


def test_scan_tolerates_malformed_lines(claude_home: Path) -> None:
    now = datetime.now(UTC)
    _session(claude_home, "mixed.jsonl", [
        _user_entry(timestamp=now - timedelta(minutes=1)),
        "{not-json",
        '{"type":"user"}',  # no timestamp; dropped
        "",
        _user_entry(timestamp=now - timedelta(minutes=2)),
    ])
    result = scan_usage(window_hours=5, home=claude_home)
    assert result.count == 2


def test_scan_ignores_tool_result_feeds(claude_home: Path) -> None:
    """Regression: pre-fix the scanner counted every assistant entry
    which over-counted tool-heavy sessions 5-20x. Ensure the new
    scanner ignores tool_result user feeds + assistant rows when
    tallying the message counter."""
    now = datetime.now(UTC)
    _session(claude_home, "heavy-tools.jsonl", [
        _user_entry(timestamp=now - timedelta(minutes=30)),          # +1
        _assistant_entry(timestamp=now - timedelta(minutes=29)),     # 0
        _tool_result_user_entry(timestamp=now - timedelta(minutes=28)),  # 0
        _assistant_entry(timestamp=now - timedelta(minutes=27)),     # 0
        _tool_result_user_entry(timestamp=now - timedelta(minutes=26)),  # 0
        _assistant_entry(timestamp=now - timedelta(minutes=25)),     # 0
    ])
    result = scan_usage(window_hours=5, home=claude_home)
    assert result.count == 1, (
        f"expected 1 operator prompt; got {result.count} — scanner is "
        "counting tool feeds or assistant rounds again"
    )
