"""Read-only surface for the coding engines layer.

  GET /coding/engines   which engines are configured and whether each
                        is currently available
  GET /coding/skills    inventory of ~/.claude/skills and
                        ~/.claude/plugins (the ambient context the
                        Claude Code bridge inherits on every call)

No write actions live here — actual runs go through the `code_task`
tool so they respect the approval gate and the plan/cost ledger.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request

from core.coding.skills_inventory import inventory as inventory_skills

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


@router.get("/skills")
async def list_skills(_request: Request) -> dict:
    """Enumerate what Claude Code will pick up ambiently on next run.

    Purely informational: PILK never reads / writes under ``~/.claude``.
    Missing directories (cloud deploys, fresh Macs) return empty lists
    rather than errors.
    """
    inv = inventory_skills()
    return {
        "skills": [asdict(p) for p in inv["skills"]],
        "plugins": [asdict(p) for p in inv["plugins"]],
    }
