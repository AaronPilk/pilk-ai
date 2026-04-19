"""Tier-0 rule tests. No LLM, no network, no sleeping."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.db.migrations import ensure_schema
from core.sentinel.heartbeats import HeartbeatStore
from core.sentinel.rules import (
    BUILTIN_RULES,
    ERROR_BURST_THRESHOLD,
    LogLine,
    build_context,
    crash_signature,
    duplicate_work,
    error_burst,
    run_rules,
    schema_violation,
    stale_heartbeat,
    stuck_task,
)


@pytest.fixture
def heartbeats(tmp_path: Path) -> HeartbeatStore:
    db = tmp_path / "pilk.db"
    ensure_schema(db)
    return HeartbeatStore(db)


@pytest.mark.asyncio
async def test_stale_heartbeat_fires_after_2x_interval(
    heartbeats: HeartbeatStore,
) -> None:
    heartbeats.upsert(
        agent_name="miner",
        status="ok",
        interval_seconds=30,
    )
    # now = 70s after last heartbeat = > 60s threshold.
    now = datetime.now(UTC) + timedelta(seconds=70)
    ctx = build_context(heartbeats=heartbeats, now=now)
    out = await stale_heartbeat(ctx)
    assert len(out) == 1
    assert out[0].kind == "stale_heartbeat"
    assert out[0].agent_name == "miner"


@pytest.mark.asyncio
async def test_stale_heartbeat_skips_disabled_agent(
    heartbeats: HeartbeatStore,
) -> None:
    heartbeats.upsert(
        agent_name="miner", status="disabled", interval_seconds=30
    )
    now = datetime.now(UTC) + timedelta(seconds=120)
    ctx = build_context(heartbeats=heartbeats, now=now)
    out = await stale_heartbeat(ctx)
    assert out == []


@pytest.mark.asyncio
async def test_stale_heartbeat_within_window_silent(
    heartbeats: HeartbeatStore,
) -> None:
    heartbeats.upsert(
        agent_name="miner", status="ok", interval_seconds=60
    )
    # 45s age < 120s = 2x60 → nothing.
    now = datetime.now(UTC) + timedelta(seconds=45)
    ctx = build_context(heartbeats=heartbeats, now=now)
    out = await stale_heartbeat(ctx)
    assert out == []


@pytest.mark.asyncio
async def test_error_burst_counts_within_window(
    heartbeats: HeartbeatStore,
) -> None:
    now = datetime.now(UTC)
    logs = defaultdict(lambda: deque(maxlen=200))
    for i in range(ERROR_BURST_THRESHOLD):
        logs["agentX"].append(
            LogLine(
                agent_name="agentX",
                level="error",
                kind="tool_error",
                message=f"fail #{i}",
                at=now - timedelta(seconds=30),
            )
        )
    ctx = build_context(heartbeats=heartbeats, logs_by_agent=logs, now=now)
    out = await error_burst(ctx)
    assert len(out) == 1
    assert out[0].kind == "error_burst"
    assert out[0].details["count"] >= ERROR_BURST_THRESHOLD


@pytest.mark.asyncio
async def test_error_burst_ignores_non_error_levels(
    heartbeats: HeartbeatStore,
) -> None:
    now = datetime.now(UTC)
    logs = defaultdict(lambda: deque(maxlen=200))
    for _ in range(ERROR_BURST_THRESHOLD * 2):
        logs["agentX"].append(
            LogLine(
                agent_name="agentX",
                level="warning",
                kind="tool_error",
                message="warn",
                at=now,
            )
        )
    ctx = build_context(heartbeats=heartbeats, logs_by_agent=logs, now=now)
    out = await error_burst(ctx)
    assert out == []


@pytest.mark.asyncio
async def test_error_burst_expires_old_lines(
    heartbeats: HeartbeatStore,
) -> None:
    now = datetime.now(UTC)
    logs = defaultdict(lambda: deque(maxlen=200))
    # All lines outside the 60s window.
    for _i in range(ERROR_BURST_THRESHOLD + 2):
        logs["agentX"].append(
            LogLine(
                agent_name="agentX",
                level="error",
                kind="x",
                message="old",
                at=now - timedelta(seconds=300),
            )
        )
    ctx = build_context(heartbeats=heartbeats, logs_by_agent=logs, now=now)
    out = await error_burst(ctx)
    assert out == []


@pytest.mark.asyncio
async def test_crash_signature_matches_traceback(
    heartbeats: HeartbeatStore,
) -> None:
    logs = defaultdict(lambda: deque(maxlen=200))
    logs["worker"].append(
        LogLine(
            agent_name="worker",
            level="error",
            kind="runtime",
            message="Traceback (most recent call last): ValueError: x",
        )
    )
    ctx = build_context(heartbeats=heartbeats, logs_by_agent=logs)
    out = await crash_signature(ctx)
    assert len(out) == 1
    assert out[0].kind == "crash_signature"


@pytest.mark.asyncio
async def test_crash_signature_matches_rate_limit(
    heartbeats: HeartbeatStore,
) -> None:
    logs = defaultdict(lambda: deque(maxlen=200))
    logs["worker"].append(
        LogLine(
            agent_name="worker",
            level="error",
            kind="http",
            message="status=429 rate-limited by upstream",
        )
    )
    ctx = build_context(heartbeats=heartbeats, logs_by_agent=logs)
    out = await crash_signature(ctx)
    assert len(out) == 1


@pytest.mark.asyncio
async def test_crash_signature_only_checks_most_recent_line(
    heartbeats: HeartbeatStore,
) -> None:
    logs = defaultdict(lambda: deque(maxlen=200))
    logs["worker"].extend(
        [
            LogLine(
                agent_name="worker",
                level="error",
                kind="x",
                message="Traceback old",
            ),
            LogLine(
                agent_name="worker",
                level="info",
                kind="x",
                message="all good",
            ),
        ]
    )
    ctx = build_context(heartbeats=heartbeats, logs_by_agent=logs)
    out = await crash_signature(ctx)
    assert out == []


@pytest.mark.asyncio
async def test_stuck_task_fires_past_timeout(
    heartbeats: HeartbeatStore,
) -> None:
    heartbeats.upsert(
        agent_name="slowpoke",
        status="ok",
        active_task_id="task-9",
        stuck_task_timeout_seconds=60,
        interval_seconds=30,
    )
    now = datetime.now(UTC) + timedelta(seconds=120)
    ctx = build_context(heartbeats=heartbeats, now=now)
    out = await stuck_task(ctx)
    assert len(out) == 1
    assert out[0].details["task_id"] == "task-9"


@pytest.mark.asyncio
async def test_stuck_task_skips_without_active_task(
    heartbeats: HeartbeatStore,
) -> None:
    heartbeats.upsert(
        agent_name="idle",
        status="ok",
        active_task_id=None,
        stuck_task_timeout_seconds=60,
    )
    now = datetime.now(UTC) + timedelta(seconds=300)
    ctx = build_context(heartbeats=heartbeats, now=now)
    out = await stuck_task(ctx)
    assert out == []


@pytest.mark.asyncio
async def test_duplicate_work_fires_on_two_claimants(
    heartbeats: HeartbeatStore,
) -> None:
    claims = {"task-7": {"a", "b"}}
    ctx = build_context(heartbeats=heartbeats, claims_by_task=claims)
    out = await duplicate_work(ctx)
    assert len(out) == 1
    assert out[0].details["agents"] == ["a", "b"]


@pytest.mark.asyncio
async def test_duplicate_work_silent_for_single_claim(
    heartbeats: HeartbeatStore,
) -> None:
    claims = {"task-7": {"a"}}
    ctx = build_context(heartbeats=heartbeats, claims_by_task=claims)
    out = await duplicate_work(ctx)
    assert out == []


@pytest.mark.asyncio
async def test_schema_violation_missing_required_key(
    heartbeats: HeartbeatStore,
) -> None:
    blobs = {"agentY": {"state": "OK"}}  # missing agent_name + updated_at
    ctx = build_context(heartbeats=heartbeats, agent_state_blobs=blobs)
    out = await schema_violation(ctx)
    assert len(out) == 1
    assert "agent_name" in out[0].details["missing_keys"]


@pytest.mark.asyncio
async def test_schema_violation_clean_blob(
    heartbeats: HeartbeatStore,
) -> None:
    blobs = {
        "agentY": {
            "agent_name": "agentY",
            "state": "OK",
            "updated_at": "now",
        }
    }
    ctx = build_context(heartbeats=heartbeats, agent_state_blobs=blobs)
    out = await schema_violation(ctx)
    assert out == []


@pytest.mark.asyncio
async def test_run_rules_dedupes_by_key(heartbeats: HeartbeatStore) -> None:
    from core.sentinel.contracts import Finding

    async def rule_a(ctx):
        return [Finding(kind="x", agent_name="a", summary="1", dedupe_key="k1")]

    async def rule_b(ctx):
        return [Finding(kind="x", agent_name="a", summary="2", dedupe_key="k1")]

    ctx = build_context(heartbeats=heartbeats)
    out = await run_rules(ctx, rules=[rule_a, rule_b])
    assert len(out) == 1
    assert out[0].summary == "1"


@pytest.mark.asyncio
async def test_run_rules_swallows_rule_exception(
    heartbeats: HeartbeatStore,
) -> None:
    async def bad_rule(ctx):
        raise RuntimeError("boom")

    ctx = build_context(heartbeats=heartbeats)
    out = await run_rules(ctx, rules=[bad_rule])
    assert len(out) == 1
    assert out[0].kind == "rule_error"


def test_builtin_rules_present() -> None:
    names = {r.__name__ for r in BUILTIN_RULES}
    assert {
        "stale_heartbeat",
        "error_burst",
        "crash_signature",
        "stuck_task",
        "duplicate_work",
        "schema_violation",
    }.issubset(names)
