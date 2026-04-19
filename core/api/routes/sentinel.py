"""HTTP surface for the sentinel supervisor.

    GET  /sentinel/summary
        Compact snapshot for the UI top-bar badge. Returns unacked
        count + top-N unacked incidents. Cheap — a single indexed
        query against ``sentinel_incidents``.

    GET  /sentinel/incidents?limit=&only_unacked=&min_severity=
        Full listing for the Incidents panel. Mirrors
        :meth:`IncidentStore.recent` with URL-parameterised filters.

    POST /sentinel/incidents/{id}/acknowledge
        Mark an incident acknowledged. Idempotent (already-acked
        returns ``acked=false``). Broadcasts ``sentinel.incident.acked``
        so the top-bar badge can decrement in real time.

All three routes sit inside the Supabase-JWT middleware — operator-only,
same posture as ``/agents`` and ``/plans``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.logging import get_logger
from core.sentinel.contracts import Incident, Severity
from core.sentinel.incidents import IncidentStore

log = get_logger("pilkd.sentinel.routes")

router = APIRouter(prefix="/sentinel")

# Small default — the top bar surfaces "what's on fire right now",
# not a full log reader. Panel callers bump the limit explicitly.
DEFAULT_SUMMARY_LIMIT = 5
MAX_LIMIT = 200


def _store(request: Request) -> IncidentStore:
    store = getattr(request.app.state, "sentinel_incidents", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="sentinel_incidents store offline"
        )
    return store


def _to_public(inc: Incident) -> dict[str, Any]:
    """Shape everyone consumes: badge, panel, broadcast payloads.

    Kept in sync with the WebSocket ``sentinel.incident`` emit in
    :func:`core.sentinel.supervisor._incident_broadcast_payload`. If
    you add a field here, mirror it there so the UI sees the same
    data whether it read from the API or the socket."""
    return {
        "id": inc.id,
        "agent": inc.agent_name,
        "severity": inc.severity.value,
        "category": inc.category.value,
        "kind": inc.finding_kind,
        "summary": inc.summary,
        "likely_cause": inc.triage.likely_cause if inc.triage else None,
        "recommended_action": (
            inc.triage.recommended_action if inc.triage else None
        ),
        "remediation": inc.remediation,
        "outcome": inc.outcome,
        "acknowledged_at": inc.acknowledged_at,
        "created_at": inc.created_at,
    }


def _parse_severity(raw: str | None) -> Severity | None:
    if raw is None:
        return None
    try:
        return Severity(raw.lower())
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=(
                f"min_severity must be one of "
                f"{[s.value for s in Severity]}"
            ),
        ) from e


@router.get("/summary")
async def summary(request: Request) -> dict[str, Any]:
    """UI top-bar badge data: count of unacked incidents + their
    compact shape for a hover/dropdown preview."""
    store = _store(request)
    unacked = store.recent(
        limit=DEFAULT_SUMMARY_LIMIT, only_unacked=True
    )
    # Total unacked (not just the top N) drives the badge number.
    all_unacked = store.recent(limit=MAX_LIMIT, only_unacked=True)
    return {
        "unacked_count": len(all_unacked),
        "top_unacked": [_to_public(i) for i in unacked],
    }


@router.get("/incidents")
async def list_incidents(
    request: Request,
    limit: int = 50,
    only_unacked: bool = False,
    min_severity: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Full incidents panel. Defaults to the 50 most recent, any
    severity, both acked + unacked."""
    if limit < 1 or limit > MAX_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be 1..{MAX_LIMIT}",
        )
    store = _store(request)
    rows = store.recent(
        limit=limit,
        only_unacked=only_unacked,
        min_severity=_parse_severity(min_severity),
        agent_name=agent_name,
    )
    return {"incidents": [_to_public(i) for i in rows]}


class AcknowledgeBody(BaseModel):
    reason: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Optional free-text note for the operator's own records. "
            "Not persisted to sentinel_incidents today; reserved for a "
            "future audit trail."
        ),
    )


@router.post("/incidents/{incident_id}/acknowledge")
async def acknowledge(
    incident_id: str,
    request: Request,
    body: AcknowledgeBody | None = None,
) -> dict[str, Any]:
    store = _store(request)
    acked = store.acknowledge(incident_id)
    log.info(
        "sentinel_incident_acked",
        incident_id=incident_id,
        newly_acked=acked,
        reason=(body.reason if body else None),
    )
    broadcast = getattr(request.app.state, "broadcast", None)
    if acked and broadcast is not None:
        try:
            await broadcast(
                "sentinel.incident.acked",
                {"id": incident_id},
            )
        except Exception as e:
            log.warning(
                "sentinel_ack_broadcast_failed",
                incident_id=incident_id,
                error=str(e),
            )
    return {"id": incident_id, "acked": acked}
