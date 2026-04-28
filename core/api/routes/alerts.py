"""HTTP surface for proactive alerts.

  GET   /alerts                      list recent events (audit trail)
  GET   /alerts/settings             current settings + defaults
  PUT   /alerts/settings             partial update
  GET   /alerts/topics                per-topic overrides
  PUT   /alerts/topics/{slug}        upsert override
  POST  /alerts/dispatch              operator-pulled: route a candidate

All write surfaces respect the conservative default posture:
- Telegram pushes only fire when ``telegram_enabled`` is true
- Setting unknown keys returns 400, not silently no-op
- Unknown topic mode returns 400
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.alerts import AlertCandidate
from core.logging import get_logger

log = get_logger("pilkd.routes.alerts")
router = APIRouter(prefix="/alerts")


def _store(request: Request):
    s = getattr(request.app.state, "alert_store", None)
    if s is None:
        raise HTTPException(503, "alerts not initialised")
    return s


def _settings(request: Request):
    s = getattr(request.app.state, "alert_settings", None)
    if s is None:
        raise HTTPException(503, "alerts not initialised")
    return s


def _topics(request: Request):
    s = getattr(request.app.state, "alert_topic_overrides", None)
    if s is None:
        raise HTTPException(503, "alerts not initialised")
    return s


def _router_(request: Request):
    s = getattr(request.app.state, "alert_router", None)
    if s is None:
        raise HTTPException(503, "alerts not initialised")
    return s


@router.get("")
async def list_alerts(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    delivery: str | None = Query(default=None),
    kind: str | None = Query(default=None),
) -> dict[str, Any]:
    store = _store(request)
    events = await store.list_recent(
        limit=limit, delivery=delivery, kind=kind,
    )
    return {
        "alerts": [
            {
                "id": e.id, "kind": e.kind, "severity": e.severity,
                "title": e.title, "body": e.body,
                "project_slug": e.project_slug,
                "topic_slug": e.topic_slug,
                "source_slug": e.source_slug,
                "score": e.score, "dedupe_key": e.dedupe_key,
                "delivery": e.delivery,
                "delivered_at": e.delivered_at,
                "metadata": e.metadata,
                "created_at": e.created_at,
            }
            for e in events
        ],
        "count": len(events),
    }


@router.get("/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    snap = await _settings(request).get()
    return snap.to_dict()


class _UpdateSettings(BaseModel):
    telegram_enabled: bool | None = Field(default=None)
    daily_brief_scheduled: bool | None = Field(default=None)
    weekly_brief_scheduled: bool | None = Field(default=None)
    digest_only: bool | None = Field(default=None)
    max_per_day: int | None = Field(default=None, ge=0, le=200)
    min_score: int | None = Field(default=None, ge=0, le=100)
    quiet_hours: str | None = Field(default=None, max_length=24)


@router.put("/settings")
async def update_settings(
    request: Request, payload: _UpdateSettings,
) -> dict[str, Any]:
    changes = {
        k: v for k, v in payload.model_dump().items() if v is not None
    }
    try:
        snap = await _settings(request).update(**changes)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return snap.to_dict()


@router.get("/topics")
async def list_topics(request: Request) -> dict[str, Any]:
    overrides = await _topics(request).list()
    return {
        "overrides": [
            {
                "topic_slug": t.topic_slug,
                "mode": t.mode,
                "mute_until": t.mute_until,
                "updated_at": t.updated_at,
            }
            for t in overrides
        ],
        "count": len(overrides),
    }


class _TopicUpdate(BaseModel):
    mode: Literal["digest", "push", "mute"] = "digest"
    mute_until: str | None = None


@router.put("/topics/{topic_slug}")
async def upsert_topic(
    request: Request, topic_slug: str, payload: _TopicUpdate,
) -> dict[str, Any]:
    try:
        t = await _topics(request).upsert(
            topic_slug=topic_slug,
            mode=payload.mode,
            mute_until=payload.mute_until,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "topic_slug": t.topic_slug,
        "mode": t.mode,
        "mute_until": t.mute_until,
        "updated_at": t.updated_at,
    }


class _Dispatch(BaseModel):
    kind: str = Field(..., max_length=64)
    title: str = Field(..., max_length=200)
    body: str | None = Field(default=None, max_length=2000)
    severity: Literal["info", "low", "med", "high", "critical"] = "info"
    project_slug: str | None = Field(default=None, max_length=64)
    topic_slug: str | None = Field(default=None, max_length=64)
    source_slug: str | None = Field(default=None, max_length=64)
    score: int | None = Field(default=None, ge=0, le=100)
    dedupe_seed: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] | None = None


@router.post("/dispatch")
async def dispatch(
    request: Request, payload: _Dispatch,
) -> dict[str, Any]:
    """Operator-pulled — route a candidate alert through the router
    and return the recorded event + decision. Useful for the UI's
    'Test alert' button and for triggers that build candidates."""
    candidate = AlertCandidate(
        kind=payload.kind,
        title=payload.title,
        body=payload.body,
        severity=payload.severity,
        project_slug=payload.project_slug,
        topic_slug=payload.topic_slug,
        source_slug=payload.source_slug,
        score=payload.score,
        dedupe_seed=payload.dedupe_seed,
        metadata=payload.metadata or {},
    )
    decision = await _router_(request).route(candidate)
    return {
        "delivery": decision.delivery,
        "reason": decision.reason,
        "event_id": decision.event.id if decision.event else None,
    }
