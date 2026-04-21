"""REST surface for timers.

  GET    /timers                 list active + recent (fired/cancelled)
  POST   /timers                 manual one-shot (UI + curl for debugging)
  DELETE /timers/{id}            cancel an active timer

The ``timer_set`` tool is the primary producer; these endpoints are
the UI + curl surface for "what reminders are queued / kill this one
before it fires."
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.timers.store import MAX_TIMER_MINUTES

router = APIRouter(prefix="/timers")


class CreateBody(BaseModel):
    minutes: int = Field(gt=0, le=MAX_TIMER_MINUTES)
    message: str


@router.get("")
async def list_timers(request: Request) -> dict[str, Any]:
    store = getattr(request.app.state, "timers", None)
    if store is None:
        return {"active": [], "recent": []}
    active = await store.list_active()
    recent = await store.list_recent(limit=50)
    return {
        "active": [t.public_dict() for t in active],
        "recent": [t.public_dict() for t in recent],
    }


@router.post("")
async def create_timer(
    body: CreateBody, request: Request,
) -> dict[str, Any]:
    store = getattr(request.app.state, "timers", None)
    if store is None:
        raise HTTPException(status_code=503, detail="timer store offline")
    fires_at = datetime.now(UTC) + timedelta(minutes=body.minutes)
    try:
        timer = await store.create(
            fires_at=fires_at,
            message=body.message,
            source="api",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return timer.public_dict()


@router.delete("/{timer_id}")
async def cancel_timer(timer_id: str, request: Request) -> dict[str, Any]:
    store = getattr(request.app.state, "timers", None)
    if store is None:
        raise HTTPException(status_code=503, detail="timer store offline")
    cancelled = await store.cancel(timer_id)
    if not cancelled:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no active timer with id {timer_id} — either it "
                "doesn't exist, already fired, or was already cancelled."
            ),
        )
    return {"id": timer_id, "cancelled": True}
