"""Governor HTTP surface.

  GET  /governor/status     current tier config + override + budget snapshot
  POST /governor/override   set the session-level tier override
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/governor")


class OverrideBody(BaseModel):
    mode: Literal["auto", "light", "standard", "premium"]


@router.get("/status")
async def governor_status(request: Request) -> dict:
    gov = getattr(request.app.state, "governor", None)
    if gov is None:
        return {"enabled": False}
    snap = await gov.snapshot()
    snap["enabled"] = True
    return snap


@router.post("/override")
async def governor_override(body: OverrideBody, request: Request) -> dict:
    gov = getattr(request.app.state, "governor", None)
    if gov is None:
        raise HTTPException(status_code=503, detail="governor offline")
    gov.set_override(body.mode)
    return {"override": body.mode}
