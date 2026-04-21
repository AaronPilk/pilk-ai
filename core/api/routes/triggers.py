"""REST surface for the trigger subsystem.

  GET  /triggers                      list registered triggers
  POST /triggers/{name}/enable        mark trigger active
  POST /triggers/{name}/disable       mark trigger inactive (skips evals)
  POST /triggers/{name}/fire          manual one-shot (ignores schedule)

The UI Settings → Triggers tab consumes this; the scheduler itself
never hits the HTTP layer (it talks to the registry directly).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from core.triggers import TriggerNotFoundError

router = APIRouter(prefix="/triggers")


@router.get("")
async def list_triggers(request: Request) -> dict:
    registry = getattr(request.app.state, "triggers", None)
    if registry is None:
        return {"triggers": []}
    return {"triggers": await registry.list_rows()}


@router.post("/{name}/enable")
async def enable_trigger(name: str, request: Request) -> dict:
    registry = getattr(request.app.state, "triggers", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="trigger registry offline")
    try:
        await registry.set_enabled(name, True)
    except TriggerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"name": name, "enabled": True}


@router.post("/{name}/disable")
async def disable_trigger(name: str, request: Request) -> dict:
    registry = getattr(request.app.state, "triggers", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="trigger registry offline")
    try:
        await registry.set_enabled(name, False)
    except TriggerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"name": name, "enabled": False}


@router.post("/{name}/fire")
async def fire_trigger(name: str, request: Request) -> dict:
    scheduler = getattr(request.app.state, "trigger_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="trigger scheduler offline")
    try:
        summary = await scheduler.fire_now(name)
    except TriggerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"name": name, **summary}
