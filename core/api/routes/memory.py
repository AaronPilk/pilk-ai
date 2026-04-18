"""HTTP surface for the structured memory store.

  GET    /memory                list entries, optional ?kind=
  POST   /memory                body: {kind, title, body?} → add
  DELETE /memory/{id}           delete one entry
  DELETE /memory                clear all (or ?kind=…)

All writes broadcast on the existing hub so other dashboards re-hydrate.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.memory import MemoryKind, MemoryStore

router = APIRouter(prefix="/memory")


class AddBody(BaseModel):
    kind: Literal[
        "preference", "standing_instruction", "fact", "pattern"
    ] = Field(description="one of: preference, standing_instruction, fact, pattern")
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=8000)


def _store(request: Request) -> MemoryStore:
    store = getattr(request.app.state, "memory", None)
    if store is None:
        raise HTTPException(status_code=503, detail="memory store offline")
    return store


async def _broadcast(request: Request, event_type: str, payload: dict) -> None:
    hub = getattr(request.app.state, "hub", None)
    if hub is not None:
        await hub.broadcast(event_type, payload)


@router.get("")
async def list_entries(request: Request, kind: str | None = None) -> dict:
    store = _store(request)
    try:
        entries = await store.list(kind=kind)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "entries": [e.public_dict() for e in entries],
        "kinds": [k.value for k in MemoryKind],
    }


@router.post("")
async def add_entry(body: AddBody, request: Request) -> dict:
    store = _store(request)
    try:
        entry = await store.add(kind=body.kind, title=body.title, body=body.body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    public = entry.public_dict()
    await _broadcast(request, "memory.created", public)
    return public


@router.delete("/{entry_id}")
async def delete_entry(entry_id: str, request: Request) -> dict:
    store = _store(request)
    removed = await store.delete(entry_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"no such entry: {entry_id}")
    await _broadcast(request, "memory.deleted", {"id": entry_id})
    return {"deleted": entry_id}


@router.delete("")
async def clear_entries(request: Request, kind: str | None = None) -> dict:
    store = _store(request)
    try:
        count = await store.clear(kind=kind)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await _broadcast(request, "memory.cleared", {"kind": kind, "count": count})
    return {"cleared": count, "kind": kind}
