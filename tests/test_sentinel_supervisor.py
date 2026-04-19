"""Supervisor end-to-end tests + Hub.subscribe plumbing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.api.hub import Hub
from core.db.migrations import ensure_schema
from core.sentinel.contracts import Category
from core.sentinel.heartbeats import HeartbeatStore
from core.sentinel.incidents import IncidentStore
from core.sentinel.notify import Notifier
from core.sentinel.remediate import RemediationResult
from core.sentinel.supervisor import DEDUPE_WINDOW_SECONDS, Supervisor


@pytest.fixture
def env(tmp_path: Path):
    db = tmp_path / "pilk.db"
    ensure_schema(db)
    heartbeats = HeartbeatStore(db)
    incidents = IncidentStore(db_path=db, jsonl_path=tmp_path / "inc.jsonl")
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    async def restart(agent_name: str) -> RemediationResult:
        return RemediationResult(
            kind="restarted", ok=True, message=f"restart {agent_name}"
        )

    supervisor = Supervisor(
        heartbeats=heartbeats,
        incidents=incidents,
        notifier=Notifier(webhook_url=None),
        restart_fn=restart,
        logs_dir=logs_dir,
        scan_interval_seconds=3600,  # effectively off for tests
    )
    return heartbeats, incidents, supervisor


@pytest.mark.asyncio
async def test_supervisor_creates_incident_on_stale_heartbeat(env) -> None:
    heartbeats, incidents, supervisor = env
    # Inject a high-confidence LLM stub so the remediation path runs —
    # heuristic triage is deliberately low-confidence and gated out.
    import json as _json

    async def stub_llm(prompt: str) -> str:
        return _json.dumps(
            {
                "severity": "high",
                "category": "stale_heartbeat",
                "likely_cause": "agent crashed",
                "recommended_action": "restart",
                "confidence": 0.9,
            }
        )

    supervisor._llm_call = stub_llm
    heartbeats.upsert(agent_name="x", status="ok", interval_seconds=30)
    # Force the scan to believe 120s have passed.
    created = await _scan_with_future(supervisor, seconds=120)
    assert len(created) == 1
    assert created[0].category == Category.STALE_HEARTBEAT
    recent = incidents.recent(agent_name="x")
    assert len(recent) == 1
    assert recent[0].remediation == "restarted"
    assert recent[0].outcome == "ok"


@pytest.mark.asyncio
async def test_supervisor_heuristic_skips_remediation(env) -> None:
    """Default heuristic triage is low-confidence; the remediation gate
    must refuse to auto-fix (operator review wanted instead)."""
    heartbeats, _incidents, supervisor = env
    heartbeats.upsert(agent_name="y", status="ok", interval_seconds=30)
    created = await _scan_with_future(supervisor, seconds=120)
    assert len(created) == 1
    assert created[0].remediation is None


@pytest.mark.asyncio
async def test_supervisor_dedupes_repeated_finding(env) -> None:
    heartbeats, incidents, supervisor = env
    heartbeats.upsert(agent_name="x", status="ok", interval_seconds=30)
    a = await _scan_with_future(supervisor, seconds=120)
    b = await _scan_with_future(supervisor, seconds=130)
    assert len(a) == 1
    assert len(b) == 0
    assert len(incidents.recent(agent_name="x")) == 1


@pytest.mark.asyncio
async def test_supervisor_dedupe_window_expires(env) -> None:
    heartbeats, _incidents, supervisor = env
    heartbeats.upsert(agent_name="x", status="ok", interval_seconds=30)
    await _scan_with_future(supervisor, seconds=120)
    # Hack the dedupe map: age the entry past the window.
    for k in list(supervisor._dedupe):
        supervisor._dedupe[k] -= DEDUPE_WINDOW_SECONDS + 10
    b = await _scan_with_future(supervisor, seconds=180)
    assert len(b) == 1


@pytest.mark.asyncio
async def test_supervisor_unknown_category_does_not_remediate(env) -> None:
    _heartbeats, incidents, supervisor = env
    # Feed the log buffer with a crash trace so the crash_signature
    # rule fires. Default heuristic triage maps it to CRASH_SIGNATURE
    # which has no remediation → incident has no remediation.
    from core.sentinel.rules import LogLine

    supervisor._logs_by_agent["worker"].append(
        LogLine(
            agent_name="worker",
            level="error",
            kind="x",
            message="Traceback (most recent call last): KaboomError",
        )
    )
    await _scan_with_future(supervisor, seconds=0)
    recent = incidents.recent(agent_name="worker")
    assert len(recent) == 1
    assert recent[0].remediation is None


@pytest.mark.asyncio
async def test_supervisor_ingests_plan_events_into_log_buffer(env) -> None:
    _heartbeats, _incidents, supervisor = env
    await supervisor.on_event(
        "plan.step_updated",
        {
            "agent": "w",
            "status": "error",
            "message": "tool x failed",
            "level": "error",
        },
    )
    # Two events with same agent + error level.
    await supervisor.on_event(
        "plan.step_updated",
        {"agent": "w", "status": "error", "message": "tool x failed again"},
    )
    buf = supervisor._logs_by_agent["w"]
    assert len(buf) == 2
    assert all(line.level == "error" for line in buf)


@pytest.mark.asyncio
async def test_supervisor_task_claim_tracking(env) -> None:
    _heartbeats, _incidents, supervisor = env
    await supervisor.on_event(
        "plan.step_updated",
        {"agent": "a", "task_id": "t-1", "status": "running"},
    )
    await supervisor.on_event(
        "plan.step_updated",
        {"agent": "b", "task_id": "t-1", "status": "running"},
    )
    assert supervisor._claims_by_task["t-1"] == {"a", "b"}
    # Completing the task clears the claim set.
    await supervisor.on_event(
        "plan.completed", {"agent": "a", "task_id": "t-1", "status": "ok"}
    )
    assert "t-1" not in supervisor._claims_by_task


@pytest.mark.asyncio
async def test_supervisor_token_ceiling_forces_heuristic(env) -> None:
    heartbeats, _incidents, supervisor = env
    # Set the ceiling to zero and confirm the triage path never calls
    # the LLM.
    called = {"n": 0}

    async def fake_llm(prompt: str) -> str:
        called["n"] += 1
        return '{"severity":"med","category":"unknown","likely_cause":"","recommended_action":"","confidence":0.9}'

    supervisor._llm_call = fake_llm
    supervisor._token_limit = 1
    heartbeats.upsert(agent_name="x", status="ok", interval_seconds=30)
    await _scan_with_future(supervisor, seconds=120)
    assert called["n"] == 0


def test_supervisor_status_reports_stale_flag(env) -> None:
    heartbeats, _incidents, supervisor = env
    heartbeats.upsert(agent_name="x", status="ok", interval_seconds=30)
    rows = supervisor.status()
    assert len(rows) == 1
    assert rows[0]["agent_name"] == "x"
    # Fresh heartbeat: not stale.
    assert rows[0]["stale"] is False


def test_supervisor_snapshot_shape(env) -> None:
    _, _, supervisor = env
    snap = supervisor.snapshot()
    assert "rules" in snap and len(snap["rules"]) >= 6
    assert snap["scan_interval_seconds"] == 3600
    assert snap["webhook_enabled"] is False


# ── Hub.subscribe plumbing ──


@pytest.mark.asyncio
async def test_hub_subscribe_receives_broadcasts() -> None:
    hub = Hub()
    received: list[tuple[str, dict]] = []

    async def listener(event_type: str, payload: dict) -> None:
        received.append((event_type, payload))

    hub.subscribe(listener)
    await hub.broadcast("x.y", {"ok": True})
    assert received == [("x.y", {"ok": True})]


@pytest.mark.asyncio
async def test_hub_subscribe_swallows_listener_errors() -> None:
    hub = Hub()

    async def bad(event_type: str, payload: dict) -> None:
        raise RuntimeError("boom")

    hub.subscribe(bad)
    # Must not raise — Sentinel misbehaviour cannot crash the hub.
    await hub.broadcast("x", {})


@pytest.mark.asyncio
async def test_hub_unsubscribe_stops_delivery() -> None:
    hub = Hub()
    hits = []

    async def listener(*a) -> None:
        hits.append(a)

    hub.subscribe(listener)
    await hub.broadcast("a", {})
    hub.unsubscribe(listener)
    await hub.broadcast("a", {})
    assert len(hits) == 1


# ── helpers ─────────────────────────────────────────────────


async def _scan_with_future(supervisor: Supervisor, *, seconds: int):
    """Run one scan pass with the clock advanced by ``seconds``. The
    supervisor reads its own ``now`` from the RuleContext; we inject
    by monkey-setting the ``_scan``-internal path via a temporary
    swap."""
    # Directly call the private scan with a built context — matches how
    # the scan would behave if the clock jumped.
    from core.sentinel.rules import RuleContext, run_rules

    ctx = RuleContext(
        heartbeats=supervisor._heartbeats,
        logs_by_agent=supervisor._logs_by_agent,
        event=None,
        claims_by_task=supervisor._claims_by_task,
        agent_state_blobs=supervisor._agent_state_blobs,
        now=datetime.now(UTC) + timedelta(seconds=seconds),
    )
    findings = await run_rules(ctx, rules=supervisor._rules)
    created = []
    for f in findings:
        inc = await supervisor._handle_finding(f)
        if inc is not None:
            created.append(inc)
    return created
