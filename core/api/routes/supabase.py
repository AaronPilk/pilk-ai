"""Supabase foundation health route.

A single GET endpoint so operators can sanity-check that the env vars
are wired and the project is reachable. Nothing else in PILK uses this
today; the route exists so future hosted/auth batches have a known
surface to extend.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/supabase")


@router.get("/health")
async def supabase_health(request: Request) -> dict:
    client = getattr(request.app.state, "supabase", None)
    if client is None:
        return {"configured": False, "reachable": False}
    status = client.public_status()
    status["reachable"] = await client.reachable() if status["configured"] else False
    return status
