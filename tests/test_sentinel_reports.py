"""Tests for the sentinel→PILK reporting path.

Covers the three new pieces wired in this PR:

* ``Supervisor.broadcast`` — new incidents emit ``sentinel.incident``
  through the injected broadcaster.
* ``Orchestrator.sentinel_context_fn`` — when set, the returned brief
  is prepended to the system prompt for the first planner turn.
* ``/sentinel/summary``, ``/sentinel/incidents``, ``/sentinel/
  incidents/{id}/acknowledge`` — HTTP surface for the UI top-bar.
* ``_compose_sentinel_brief`` — only promotes MED+ unacked incidents
  and returns empty when there's nothing worth surfacing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.app import _compose_sentinel_brief
from core.api.routes.sentinel import router as sentinel_router
from core.config import get_settings
from core.db import ensure_schema
from core.governor.providers import AnthropicPlannerProvider
from core.ledger import Ledger
from core.orchestrator import Orchestrator, PlanStore
from core.policy import Gate
from core.sentinel.contracts import (
    Category,
    Finding,
    Severity,
    TriageResult,
)
from core.sentinel.incidents import IncidentStore
from core.tools import Gateway, ToolRegistry
from core.tools.builtin import fs_read_tool


@pytest.fixture
def incidents(tmp_path: Path) -> IncidentStore:
    db = tmp_path / "pilk.db"
    ensure_schema(db)
    return IncidentStore(db)


def _seed(
    store: IncidentStore,
    *,
    agent: str = "sales_ops_agent",
    severity: Severity = Severity.HIGH,
    summary: str = "heartbeat stale for 120s",
    cause: str | None = "agent process likely wedged on IO",
):
    finding = Finding(
        kind="stale_heartbeat",
        agent_name=agent,
        summary=summary,
        details={"age_seconds": 120},
        dedupe_key=f"stale:{agent}",
    )
    triage = TriageResult(
        severity=severity,
        category=Category.STALE_HEARTBEAT,
        likely_cause=cause or "",
        recommended_action="restart agent",
        confidence=0.9,
    )
    return store.create(
        finding=finding,
        triage=triage,
        category=triage.category,
        severity=triage.severity,
    )


# ── _compose_sentinel_brief ─────────────────────────────────────


def test_brief_empty_when_no_incidents(incidents: IncidentStore) -> None:
    assert _compose_sentinel_brief(incidents) == ""


def test_brief_includes_high_severity(incidents: IncidentStore) -> None:
    inc = _seed(incidents, severity=Severity.HIGH)
    brief = _compose_sentinel_brief(incidents)
    assert "Sentinel situation report" in brief
    assert "sales_ops_agent" in brief
    assert inc.id in brief
    assert "[high]" in brief
    assert inc.summary in brief


def test_brief_skips_low_severity_chatter(incidents: IncidentStore) -> None:
    _seed(incidents, severity=Severity.LOW, summary="one retry used")
    assert _compose_sentinel_brief(incidents) == ""


def test_brief_ignores_acknowledged(incidents: IncidentStore) -> None:
    inc = _seed(incidents, severity=Severity.HIGH)
    incidents.acknowledge(inc.id)
    assert _compose_sentinel_brief(incidents) == ""


def test_brief_cap_on_length(incidents: IncidentStore) -> None:
    """With ten unacked HIGH incidents the brief still surfaces at most
    SENTINEL_BRIEF_MAX_INCIDENTS rows so the orchestrator prompt stays
    tight."""
    from core.api.app import SENTINEL_BRIEF_MAX_INCIDENTS

    for i in range(10):
        _seed(
            incidents,
            agent=f"agent_{i}",
            severity=Severity.HIGH,
            summary=f"problem {i}",
        )
    brief = _compose_sentinel_brief(incidents)
    bullet_lines = [ln for ln in brief.splitlines() if ln.startswith("- ")]
    assert len(bullet_lines) == SENTINEL_BRIEF_MAX_INCIDENTS


# ── /sentinel/* HTTP surface ────────────────────────────────────


def _app_with_store(
    incidents: IncidentStore, *, broadcasts: list | None = None
) -> FastAPI:
    app = FastAPI()
    app.state.sentinel_incidents = incidents
    if broadcasts is not None:

        async def broadcast(event: str, payload: dict) -> None:
            broadcasts.append((event, payload))

        app.state.broadcast = broadcast
    app.include_router(sentinel_router)
    return app


def test_summary_counts_unacked_only(incidents: IncidentStore) -> None:
    acked = _seed(incidents, severity=Severity.HIGH)
    incidents.acknowledge(acked.id)
    _seed(incidents, severity=Severity.HIGH, summary="second incident")

    client = TestClient(_app_with_store(incidents))
    r = client.get("/sentinel/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["unacked_count"] == 1
    assert len(body["top_unacked"]) == 1
    assert body["top_unacked"][0]["summary"] == "second incident"


def test_list_incidents_filters_by_severity(
    incidents: IncidentStore,
) -> None:
    _seed(incidents, severity=Severity.LOW, summary="low thing")
    _seed(incidents, severity=Severity.HIGH, summary="high thing")
    client = TestClient(_app_with_store(incidents))
    r = client.get("/sentinel/incidents?min_severity=high")
    assert r.status_code == 200
    summaries = [i["summary"] for i in r.json()["incidents"]]
    assert summaries == ["high thing"]


def test_list_incidents_rejects_bad_severity(
    incidents: IncidentStore,
) -> None:
    client = TestClient(_app_with_store(incidents))
    r = client.get("/sentinel/incidents?min_severity=bogus")
    assert r.status_code == 400


def test_list_incidents_limit_bounds(incidents: IncidentStore) -> None:
    client = TestClient(_app_with_store(incidents))
    assert client.get("/sentinel/incidents?limit=0").status_code == 400
    assert client.get("/sentinel/incidents?limit=99999").status_code == 400


def test_acknowledge_flips_once_and_broadcasts(
    incidents: IncidentStore,
) -> None:
    inc = _seed(incidents, severity=Severity.HIGH)
    broadcasts: list = []
    client = TestClient(_app_with_store(incidents, broadcasts=broadcasts))

    first = client.post(f"/sentinel/incidents/{inc.id}/acknowledge")
    assert first.status_code == 200
    assert first.json() == {"id": inc.id, "acked": True}
    assert broadcasts == [("sentinel.incident.acked", {"id": inc.id})]

    second = client.post(f"/sentinel/incidents/{inc.id}/acknowledge")
    assert second.status_code == 200
    assert second.json() == {"id": inc.id, "acked": False}
    assert len(broadcasts) == 1  # no second broadcast


def test_summary_503_when_store_missing() -> None:
    app = FastAPI()
    app.include_router(sentinel_router)
    client = TestClient(app)
    r = client.get("/sentinel/summary")
    assert r.status_code == 503


# ── Supervisor.broadcast ────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_emits_sentinel_incident_on_create(
    tmp_path: Path,
) -> None:
    from core.sentinel.heartbeats import HeartbeatStore
    from core.sentinel.supervisor import Supervisor

    db = tmp_path / "pilk.db"
    ensure_schema(db)
    hbs = HeartbeatStore(db)
    incs = IncidentStore(db)
    captured: list = []

    async def broadcast(event: str, payload: dict) -> None:
        captured.append((event, payload))

    sup = Supervisor(
        heartbeats=hbs,
        incidents=incs,
        logs_dir=tmp_path,
        broadcast=broadcast,
    )

    finding = Finding(
        kind="stale_heartbeat",
        agent_name="sales_ops_agent",
        summary="heartbeat stale for 200s",
        details={"age_seconds": 200},
        dedupe_key="stale:sales_ops_agent",
    )
    await sup._handle_finding(finding)

    kinds = [e for e, _ in captured]
    assert "sentinel.incident" in kinds
    payload = dict(captured)["sentinel.incident"]
    assert payload["agent"] == "sales_ops_agent"
    assert payload["kind"] == "stale_heartbeat"
    assert "severity" in payload
    assert "id" in payload


@pytest.mark.asyncio
async def test_supervisor_swallows_broadcast_failure(
    tmp_path: Path,
) -> None:
    """A broken broadcaster must never prevent the incident from being
    persisted — sentinel is a safety net, not a tripwire."""
    from core.sentinel.heartbeats import HeartbeatStore
    from core.sentinel.supervisor import Supervisor

    db = tmp_path / "pilk.db"
    ensure_schema(db)
    hbs = HeartbeatStore(db)
    incs = IncidentStore(db)

    async def broadcast(event: str, payload: dict) -> None:
        raise RuntimeError("websocket hub exploded")

    sup = Supervisor(
        heartbeats=hbs,
        incidents=incs,
        logs_dir=tmp_path,
        broadcast=broadcast,
    )

    finding = Finding(
        kind="stale_heartbeat",
        agent_name="sales_ops_agent",
        summary="heartbeat stale",
        details={},
        dedupe_key="x",
    )
    inc = await sup._handle_finding(finding)
    assert inc is not None
    # Incident should still be on disk despite the broadcast failure.
    assert incs.recent(limit=10)[0].id == inc.id


# ── Orchestrator prepends sentinel brief to system prompt ───────


@dataclass
class _Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict | None = None


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _Response:
    content: list[_Block]
    stop_reason: str
    usage: _Usage


class _StubMessages:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _StubClient:
    def __init__(self, responses: list[_Response]) -> None:
        self.messages = _StubMessages(responses)

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_orchestrator_prepends_sentinel_brief() -> None:
    """When sentinel_context_fn returns a non-empty brief, it must
    appear in the `system` prompt that the planner sees on turn 1."""
    settings = get_settings()
    ensure_schema(settings.db_path)

    client = _StubClient(
        [
            _Response(
                content=[_Block(type="text", text="ok")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=10, output_tokens=2),
            )
        ]
    )
    ledger = Ledger(settings.db_path)
    plans = PlanStore(settings.db_path)
    registry = ToolRegistry()
    registry.register(fs_read_tool)
    gateway = Gateway(registry, Gate())

    async def broadcast(event_type: str, payload: dict) -> None:
        pass

    async def sentinel_context_fn() -> str:
        return "[Sentinel situation report]\n- [high] sales_ops_agent: stuck"

    orch = Orchestrator(
        client=client,
        registry=registry,
        gateway=gateway,
        ledger=ledger,
        plans=plans,
        broadcast=broadcast,
        planner_model="claude-opus-4-7",
        max_turns=2,
        providers={"anthropic": AnthropicPlannerProvider(client)},
        sentinel_context_fn=sentinel_context_fn,
    )

    await orch.run("say ok")

    assert client.messages.calls, "planner was never called"
    system_prompt = client.messages.calls[0]["system"]
    # `system` is a list of {type,text,cache_control} blocks with
    # cache-control on at least one — the brief lives in the combined
    # text, regardless of how the provider split it.
    if isinstance(system_prompt, list):
        joined = "".join(
            block.get("text", "")
            for block in system_prompt
            if isinstance(block, dict)
        )
    else:
        joined = str(system_prompt)
    assert "Sentinel situation report" in joined
    assert "sales_ops_agent" in joined


@pytest.mark.asyncio
async def test_orchestrator_no_op_when_brief_is_empty() -> None:
    """An empty brief should not mutate the system prompt at all — no
    'report' marker, no trailing whitespace."""
    settings = get_settings()
    ensure_schema(settings.db_path)

    client = _StubClient(
        [
            _Response(
                content=[_Block(type="text", text="ok")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=10, output_tokens=2),
            )
        ]
    )
    ledger = Ledger(settings.db_path)
    plans = PlanStore(settings.db_path)
    registry = ToolRegistry()
    registry.register(fs_read_tool)
    gateway = Gateway(registry, Gate())

    async def broadcast(event_type: str, payload: dict) -> None:
        pass

    async def sentinel_context_fn() -> str:
        return ""

    orch = Orchestrator(
        client=client,
        registry=registry,
        gateway=gateway,
        ledger=ledger,
        plans=plans,
        broadcast=broadcast,
        planner_model="claude-opus-4-7",
        max_turns=2,
        providers={"anthropic": AnthropicPlannerProvider(client)},
        sentinel_context_fn=sentinel_context_fn,
    )

    await orch.run("say ok")

    system_prompt = client.messages.calls[0]["system"]
    if isinstance(system_prompt, list):
        joined = "".join(
            block.get("text", "")
            for block in system_prompt
            if isinstance(block, dict)
        )
    else:
        joined = str(system_prompt)
    assert "Sentinel situation report" not in joined
