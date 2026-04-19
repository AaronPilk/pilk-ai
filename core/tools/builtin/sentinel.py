"""Tool surface for the Sentinel supervisor.

Four tools:

    sentinel_heartbeat          — every long-running agent calls this per loop.
    sentinel_status             — one-line health for every known agent.
    sentinel_list_incidents     — browse the persistent incident log.
    sentinel_acknowledge_incident — mark one incident human-reviewed.

``sentinel_heartbeat`` is the only one agents other than Sentinel will
ever call. The other three are for the operator (via the dashboard)
and for Sentinel itself when it needs to introspect its own state.

Factory-built tools use ``make_*`` because they close over
process-wide handles (HeartbeatStore, Supervisor, IncidentStore) that
FastAPI lifespan owns.
"""

from __future__ import annotations

from typing import Any

from core.policy.risk import RiskClass
from core.sentinel.contracts import Severity
from core.sentinel.heartbeats import (
    DEFAULT_INTERVAL_S,
    DEFAULT_STUCK_TIMEOUT_S,
    HeartbeatStore,
)
from core.sentinel.incidents import IncidentStore
from core.sentinel.supervisor import Supervisor
from core.tools.registry import Tool, ToolContext, ToolOutcome

ALLOWED_STATUSES = frozenset({"ok", "degraded", "disabled"})


def make_sentinel_heartbeat_tool(store: HeartbeatStore) -> Tool:
    async def _heartbeat(args: dict, ctx: ToolContext) -> ToolOutcome:
        agent_name = str(
            args.get("agent_name") or ctx.agent_name or ""
        ).strip()
        if not agent_name:
            return ToolOutcome(
                content=(
                    "sentinel_heartbeat requires 'agent_name' (or a "
                    "ToolContext with agent_name set)."
                ),
                is_error=True,
            )
        status = str(args.get("status") or "ok").strip().lower()
        if status not in ALLOWED_STATUSES:
            return ToolOutcome(
                content=(
                    f"status must be one of {sorted(ALLOWED_STATUSES)}, "
                    f"got '{status}'."
                ),
                is_error=True,
            )
        interval = int(args.get("interval_seconds") or DEFAULT_INTERVAL_S)
        stuck = int(
            args.get("stuck_task_timeout_seconds") or DEFAULT_STUCK_TIMEOUT_S
        )
        hb = store.upsert(
            agent_name=agent_name,
            status=status,
            progress=args.get("progress"),
            active_task_id=args.get("task_id"),
            interval_seconds=interval,
            stuck_task_timeout_seconds=stuck,
        )
        return ToolOutcome(
            content=f"heartbeat@{hb.last_at} status={hb.status}",
            data={
                "agent_name": hb.agent_name,
                "status": hb.status,
                "last_at": hb.last_at,
                "interval_seconds": hb.interval_seconds,
            },
        )

    return Tool(
        name="sentinel_heartbeat",
        description=(
            "Report liveness to the Sentinel supervisor. Long-running "
            "agents call this once per loop iteration. See "
            "agents/sentinel/CONTRACT.md for the full contract. Accepts "
            "status ∈ {ok, degraded, disabled}, optional progress "
            "string + active task_id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent_name": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": sorted(ALLOWED_STATUSES),
                },
                "progress": {"type": "string"},
                "task_id": {"type": "string"},
                "interval_seconds": {
                    "type": "integer",
                    "minimum": 5,
                    "maximum": 3600,
                },
                "stuck_task_timeout_seconds": {
                    "type": "integer",
                    "minimum": 30,
                    "maximum": 86400,
                },
            },
        },
        risk=RiskClass.READ,
        handler=_heartbeat,
    )


def make_sentinel_status_tool(supervisor: Supervisor) -> Tool:
    async def _status(args: dict, ctx: ToolContext) -> ToolOutcome:
        rows = supervisor.status()
        snap = supervisor.snapshot()
        stale = [r for r in rows if r["stale"]]
        return ToolOutcome(
            content=(
                f"{len(rows)} agent(s) tracked; "
                f"{len(stale)} stale; "
                f"token_spend={snap['token_spend']['total']}"
            ),
            data={"agents": rows, "supervisor": snap},
        )

    return Tool(
        name="sentinel_status",
        description=(
            "One-line health summary for every agent tracked by "
            "Sentinel, plus the supervisor's own stats "
            "(rules, token spend, webhook config)."
        ),
        input_schema={"type": "object", "properties": {}},
        risk=RiskClass.READ,
        handler=_status,
    )


def make_sentinel_list_incidents_tool(incidents: IncidentStore) -> Tool:
    async def _list(args: dict, ctx: ToolContext) -> ToolOutcome:
        limit = int(args.get("limit") or 25)
        limit = max(1, min(limit, 200))
        agent = args.get("agent_name")
        min_sev_raw = args.get("min_severity")
        min_sev = Severity.parse(str(min_sev_raw)) if min_sev_raw else None
        only_unacked = bool(args.get("only_unacked") or False)
        rows = incidents.recent(
            limit=limit,
            agent_name=agent,
            min_severity=min_sev,
            only_unacked=only_unacked,
        )
        return ToolOutcome(
            content=f"{len(rows)} incident(s)",
            data={
                "count": len(rows),
                "incidents": [_incident_dict(i) for i in rows],
            },
        )

    return Tool(
        name="sentinel_list_incidents",
        description=(
            "List recent Sentinel incidents, newest first. Filter by "
            "agent_name, min severity (low|med|high|critical), or "
            "only_unacked=true."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "agent_name": {"type": "string"},
                "min_severity": {
                    "type": "string",
                    "enum": [s.value for s in Severity],
                },
                "only_unacked": {"type": "boolean"},
            },
        },
        risk=RiskClass.READ,
        handler=_list,
    )


def make_sentinel_acknowledge_tool(incidents: IncidentStore) -> Tool:
    async def _ack(args: dict, ctx: ToolContext) -> ToolOutcome:
        incident_id = str(args.get("incident_id") or "").strip()
        if not incident_id:
            return ToolOutcome(
                content="sentinel_acknowledge_incident requires 'incident_id'.",
                is_error=True,
            )
        ok = incidents.acknowledge(incident_id)
        return ToolOutcome(
            content=(
                f"acknowledged {incident_id}"
                if ok
                else f"{incident_id} was already acknowledged or not found"
            ),
            data={"incident_id": incident_id, "acknowledged": ok},
        )

    return Tool(
        name="sentinel_acknowledge_incident",
        description=(
            "Mark a Sentinel incident as operator-reviewed so it stops "
            "appearing in only_unacked=true lists. Idempotent."
        ),
        input_schema={
            "type": "object",
            "properties": {"incident_id": {"type": "string"}},
            "required": ["incident_id"],
        },
        risk=RiskClass.READ,
        handler=_ack,
    )


def make_sentinel_tools(
    *,
    heartbeats: HeartbeatStore,
    incidents: IncidentStore,
    supervisor: Supervisor,
) -> list[Tool]:
    return [
        make_sentinel_heartbeat_tool(heartbeats),
        make_sentinel_status_tool(supervisor),
        make_sentinel_list_incidents_tool(incidents),
        make_sentinel_acknowledge_tool(incidents),
    ]


def _incident_dict(i: Any) -> dict[str, Any]:
    return {
        "id": i.id,
        "agent_name": i.agent_name,
        "category": i.category.value,
        "severity": i.severity.value,
        "finding_kind": i.finding_kind,
        "summary": i.summary,
        "remediation": i.remediation,
        "outcome": i.outcome,
        "acknowledged_at": i.acknowledged_at,
        "created_at": i.created_at,
        "triage": (
            {
                "severity": i.triage.severity.value,
                "category": i.triage.category.value,
                "likely_cause": i.triage.likely_cause,
                "recommended_action": i.triage.recommended_action,
                "confidence": i.triage.confidence,
            }
            if i.triage
            else None
        ),
    }


__all__ = [
    "ALLOWED_STATUSES",
    "make_sentinel_acknowledge_tool",
    "make_sentinel_heartbeat_tool",
    "make_sentinel_list_incidents_tool",
    "make_sentinel_status_tool",
    "make_sentinel_tools",
]
