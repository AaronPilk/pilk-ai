"""HTTP surface for PILK's evolving persona.

Thin wrapper around the ``persona_consolidate_agent`` + the two
vault files the agent maintains. The Settings page (and any future
"sharpen me now" button) POSTs here to kick an immediate run without
waiting for the 03:30 nightly trigger.

  POST /persona/consolidate   → fire persona_consolidate_agent now
  GET  /persona                → snapshot: both persona notes + mtime

Neither endpoint writes directly — consolidation goes through the
agent path (which in turn queues its vault writes through the
approval flow if the operator's policy requires it), and read is
pure passthrough.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from core.logging import get_logger
from core.orchestrator.orchestrator import OrchestratorBusyError
from core.registry.registry import AgentNotFoundError

log = get_logger("pilkd.persona.route")

router = APIRouter(prefix="/persona")

AGENT_NAME = "persona_consolidate_agent"
DEFAULT_TASK = (
    "Run the persona consolidation pass now. Review the last 14 days "
    "of daily notes + structured memory, read persona/pilk.md and "
    "persona/operator.md, and append dated observations per the "
    "procedure in your system prompt. On-demand run — not the nightly "
    "trigger."
)

PERSONA_PATHS: tuple[tuple[str, str], ...] = (
    ("persona/pilk.md", "pilk"),
    ("persona/operator.md", "operator"),
)


@router.get("")
async def read_persona(request: Request) -> dict[str, Any]:
    """Return the current bodies of both persona notes.

    Missing files are returned as empty strings — the UI renders that
    as "not yet written" rather than an error. Vault unavailable is a
    503 since there's nothing to render.
    """
    vault = getattr(request.app.state, "brain", None)
    if vault is None:
        raise HTTPException(status_code=503, detail="brain vault offline")
    out: dict[str, Any] = {}
    for rel, key in PERSONA_PATHS:
        try:
            body = vault.read(rel)
        except FileNotFoundError:
            body = ""
        except Exception as e:
            log.warning("persona_read_failed", path=rel, error=str(e))
            body = ""
        out[key] = {"path": rel, "body": body}
    out["fetched_at"] = datetime.now(UTC).isoformat()
    return out


@router.post("/consolidate")
async def consolidate_now(request: Request) -> dict[str, Any]:
    """Fire the persona_consolidate_agent on demand.

    Returns 202-ish shape ({"accepted": true, ...}) immediately — the
    actual plan runs in the background and emits events on the hub
    exactly like the nightly trigger does. 409 if another plan is in
    flight; 404 if the agent isn't registered (shouldn't happen in
    production, but useful during first-boot before the registry
    finishes loading).
    """
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        raise HTTPException(
            status_code=503,
            detail="orchestrator offline (set ANTHROPIC_API_KEY)",
        )
    if orchestrator.running_plan_id is not None:
        raise HTTPException(status_code=409, detail="a plan is already running")
    registry = getattr(request.app.state, "agents", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="agent registry offline")
    try:
        registry.get(AGENT_NAME)
    except AgentNotFoundError as e:
        raise HTTPException(
            status_code=404,
            detail=(
                f"{AGENT_NAME} not registered — reinstall the agent or "
                "restart pilkd after pulling the latest manifest."
            ),
        ) from e
    tasks: set = request.app.state.orchestrator_tasks
    try:
        run = asyncio.create_task(
            orchestrator.agent_run(AGENT_NAME, DEFAULT_TASK)
        )
    except OrchestratorBusyError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    tasks.add(run)
    run.add_done_callback(tasks.discard)
    log.info("persona_consolidate_fired", source="on_demand")
    return {"accepted": True, "agent": AGENT_NAME}
