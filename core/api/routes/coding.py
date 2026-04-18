"""Read-only surface for the coding engines layer.

  GET /coding/engines   which engines are configured and whether each
                        is currently available

No write actions live here — actual runs go through the `code_task`
tool so they respect the approval gate and the plan/cost ledger.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/coding")


@router.get("/engines")
async def list_engines(request: Request) -> dict:
    router_obj = getattr(request.app.state, "coding_router", None)
    if router_obj is None:
        raise HTTPException(status_code=503, detail="coding router offline")
    engines: list[dict] = []
    for name in router_obj.names():
        engine = router_obj.get(name)
        if engine is None:
            continue
        health = await engine.health()
        engines.append(
            {
                "name": health.name,
                "label": health.label,
                "available": health.available,
                "detail": health.detail,
            }
        )
    return {"engines": engines}
