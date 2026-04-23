"""Ledger.subscription_usage — the query driving the Max subscription
bar in the dashboard header.

Covers:
  * Only ``tier_provider=claude_code`` rows are counted (API calls to
    Anthropic directly or OpenAI don't inflate the count).
  * Rows older than ``window_hours`` are excluded.
  * Empty ledger returns count=0 (no crash, no None leakage).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.db import ensure_schema
from core.ledger import Ledger


def _write_entry(
    db_path, *, tier_provider: str, occurred_at: datetime, model: str = "m",
) -> None:
    """Bypass Ledger.record_llm to stamp an exact ``occurred_at``
    (the public method uses datetime.now internally)."""
    import json
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO cost_entries
              (plan_id, step_id, agent_name, kind, model,
               input_tokens, output_tokens, usd, occurred_at, metadata_json)
            VALUES (NULL, NULL, NULL, 'llm', ?, 10, 20, 0.001, ?, ?)
            """,
            (
                model,
                occurred_at.isoformat(),
                json.dumps({"tier_provider": tier_provider}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def ledger(tmp_path):
    db = tmp_path / "ledger.sqlite"
    ensure_schema(db)
    return Ledger(db)


@pytest.mark.asyncio
async def test_subscription_usage_counts_only_claude_code(ledger, tmp_path):
    now = datetime.now(UTC)
    # Recent subscription calls — counted.
    for _ in range(3):
        _write_entry(
            tmp_path / "ledger.sqlite",
            tier_provider="claude_code",
            occurred_at=now - timedelta(minutes=10),
        )
    # Recent API call — NOT counted (different provider).
    _write_entry(
        tmp_path / "ledger.sqlite",
        tier_provider="anthropic",
        occurred_at=now - timedelta(minutes=5),
    )
    # Old subscription call — outside the 5-hour window.
    _write_entry(
        tmp_path / "ledger.sqlite",
        tier_provider="claude_code",
        occurred_at=now - timedelta(hours=7),
    )

    result = await ledger.subscription_usage(window_hours=5)
    assert result["count"] == 3
    assert result["window_hours"] == 5
    assert result["oldest_at"] is not None


@pytest.mark.asyncio
async def test_subscription_usage_empty_ledger(ledger):
    result = await ledger.subscription_usage(window_hours=5)
    assert result["count"] == 0
    assert result["oldest_at"] is None


@pytest.mark.asyncio
async def test_subscription_usage_custom_window_hours(ledger, tmp_path):
    now = datetime.now(UTC)
    _write_entry(
        tmp_path / "ledger.sqlite",
        tier_provider="claude_code",
        occurred_at=now - timedelta(hours=2),
    )
    # One-hour window should exclude the 2-hour-old row.
    narrow = await ledger.subscription_usage(window_hours=1)
    assert narrow["count"] == 0
    # Three-hour window includes it.
    wide = await ledger.subscription_usage(window_hours=3)
    assert wide["count"] == 1
