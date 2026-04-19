"""Sentinel tool surface tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db.migrations import ensure_schema
from core.sentinel.heartbeats import HeartbeatStore
from core.sentinel.incidents import IncidentStore
from core.sentinel.notify import Notifier
from core.sentinel.remediate import RemediationResult
from core.sentinel.supervisor import Supervisor
from core.tools.builtin.sentinel import (
    make_sentinel_acknowledge_tool,
    make_sentinel_heartbeat_tool,
    make_sentinel_list_incidents_tool,
    make_sentinel_status_tool,
    make_sentinel_tools,
)
from core.tools.registry import ToolContext


@pytest.fixture
def env(tmp_path: Path):
    db = tmp_path / "pilk.db"
    ensure_schema(db)
    heartbeats = HeartbeatStore(db)
    incidents = IncidentStore(db_path=db, jsonl_path=None)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    async def restart(agent_name: str) -> RemediationResult:
        return RemediationResult(kind="restarted", ok=True, message="ok")

    supervisor = Supervisor(
        heartbeats=heartbeats,
        incidents=incidents,
        notifier=Notifier(webhook_url=None),
        restart_fn=restart,
        logs_dir=logs_dir,
        scan_interval_seconds=3600,
    )
    return heartbeats, incidents, supervisor


@pytest.mark.asyncio
async def test_heartbeat_requires_agent_name(env) -> None:
    heartbeats, _, _ = env
    tool = make_sentinel_heartbeat_tool(heartbeats)
    out = await tool.handler({}, ToolContext())
    assert out.is_error
    assert "agent_name" in out.content


@pytest.mark.asyncio
async def test_heartbeat_infers_agent_name_from_context(env) -> None:
    heartbeats, _, _ = env
    tool = make_sentinel_heartbeat_tool(heartbeats)
    out = await tool.handler(
        {"status": "ok", "progress": "hi"},
        ToolContext(agent_name="inferred"),
    )
    assert not out.is_error
    assert out.data["agent_name"] == "inferred"


@pytest.mark.asyncio
async def test_heartbeat_rejects_unknown_status(env) -> None:
    heartbeats, _, _ = env
    tool = make_sentinel_heartbeat_tool(heartbeats)
    out = await tool.handler(
        {"agent_name": "a", "status": "whatever"}, ToolContext()
    )
    assert out.is_error


@pytest.mark.asyncio
async def test_status_tool_returns_rows(env) -> None:
    heartbeats, _, supervisor = env
    heartbeats.upsert(agent_name="a", status="ok")
    tool = make_sentinel_status_tool(supervisor)
    out = await tool.handler({}, ToolContext())
    assert not out.is_error
    assert len(out.data["agents"]) == 1
    assert "supervisor" in out.data
    assert "token_spend" in out.data["supervisor"]


@pytest.mark.asyncio
async def test_list_incidents_tool_respects_filters(env) -> None:
    _, incidents, _ = env
    from core.sentinel.contracts import Category, Finding, Severity

    incidents.create(
        finding=Finding(kind="stale_heartbeat", agent_name="a", summary="x"),
        triage=None,
        category=Category.STALE_HEARTBEAT,
        severity=Severity.LOW,
    )
    incidents.create(
        finding=Finding(kind="crash_signature", agent_name="b", summary="x"),
        triage=None,
        category=Category.CRASH_SIGNATURE,
        severity=Severity.CRITICAL,
    )

    tool = make_sentinel_list_incidents_tool(incidents)

    all_out = await tool.handler({"limit": 50}, ToolContext())
    assert all_out.data["count"] == 2

    high_only = await tool.handler(
        {"min_severity": "high"}, ToolContext()
    )
    assert high_only.data["count"] == 1
    assert high_only.data["incidents"][0]["severity"] == "critical"


@pytest.mark.asyncio
async def test_acknowledge_tool_marks_row(env) -> None:
    _, incidents, _ = env
    from core.sentinel.contracts import Category, Finding, Severity

    inc = incidents.create(
        finding=Finding(kind="stale", agent_name="a", summary="x"),
        triage=None,
        category=Category.STALE_HEARTBEAT,
        severity=Severity.HIGH,
    )
    tool = make_sentinel_acknowledge_tool(incidents)
    out = await tool.handler(
        {"incident_id": inc.id}, ToolContext()
    )
    assert not out.is_error
    assert out.data["acknowledged"] is True


@pytest.mark.asyncio
async def test_acknowledge_missing_id_errors(env) -> None:
    _, incidents, _ = env
    tool = make_sentinel_acknowledge_tool(incidents)
    out = await tool.handler({}, ToolContext())
    assert out.is_error


@pytest.mark.asyncio
async def test_make_sentinel_tools_bundles_four(env) -> None:
    heartbeats, incidents, supervisor = env
    tools = make_sentinel_tools(
        heartbeats=heartbeats,
        incidents=incidents,
        supervisor=supervisor,
    )
    assert {t.name for t in tools} == {
        "sentinel_heartbeat",
        "sentinel_status",
        "sentinel_list_incidents",
        "sentinel_acknowledge_incident",
    }
