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


class ConfigBody(BaseModel):
    daily_cap_usd: float | None = None
    premium_gate: Literal["ask", "auto"] | None = None


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


@router.post("/config")
async def governor_config(body: ConfigBody, request: Request) -> dict:
    gov = getattr(request.app.state, "governor", None)
    if gov is None:
        raise HTTPException(status_code=503, detail="governor offline")
    if body.daily_cap_usd is not None:
        try:
            gov.set_daily_cap(body.daily_cap_usd)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    if body.premium_gate is not None:
        gov.set_premium_gate(body.premium_gate)
    return await gov.snapshot()
