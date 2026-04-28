"""HTTP surface for the Intelligence Engine — Batch 1.

Endpoints:
  Sources:
    GET    /intelligence/sources                list configured sources
    POST   /intelligence/sources                create a source
    GET    /intelligence/sources/{id}           fetch one source
    PUT    /intelligence/sources/{id}           update one source
    DELETE /intelligence/sources/{id}           remove a source
    POST   /intelligence/sources/{id}/test      preview-only fetch (no DB write)
    POST   /intelligence/sources/{id}/refresh   real fetch (writes new items)

  Topics (watchlists):
    GET    /intelligence/topics
    POST   /intelligence/topics
    GET    /intelligence/topics/{id}
    PUT    /intelligence/topics/{id}
    DELETE /intelligence/topics/{id}

  Items + runs (read-only):
    GET    /intelligence/items                  list stored items
    GET    /intelligence/runs                   recent fetch runs

Batch 1 boundaries:
  - No alerts (Telegram / dashboard / brief).
  - No autonomous fetching — operator hits /refresh manually.
  - No LLM calls.
  - Non-rss source kinds return 501 from /test and /refresh.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.intelligence.fetchers import (
    NotImplementedFetchError,
    fetch_for_source,
)
from core.intelligence.fetchers.base import FetchError
from core.intelligence.items import ItemStore
from core.intelligence.manual import ingest_manual_item
from core.intelligence.pipeline import IntelligencePipeline
from core.intelligence.sources import SourceRegistry, SourceValidationError
from core.intelligence.topics import TopicRegistry, TopicValidationError
from core.logging import get_logger

log = get_logger("pilkd.api.intelligence")

router = APIRouter(prefix="/intelligence")


# ── helpers ──────────────────────────────────────────────────────


def _sources(request: Request) -> SourceRegistry:
    s = getattr(request.app.state, "intel_sources", None)
    if s is None:
        raise HTTPException(503, "intelligence.sources offline")
    return s


def _topics(request: Request) -> TopicRegistry:
    t = getattr(request.app.state, "intel_topics", None)
    if t is None:
        raise HTTPException(503, "intelligence.topics offline")
    return t


def _items(request: Request) -> ItemStore:
    i = getattr(request.app.state, "intel_items", None)
    if i is None:
        raise HTTPException(503, "intelligence.items offline")
    return i


def _pipeline(request: Request) -> IntelligencePipeline:
    p = getattr(request.app.state, "intel_pipeline", None)
    if p is None:
        raise HTTPException(503, "intelligence.pipeline offline")
    return p


def _source_to_dict(s: Any) -> dict[str, Any]:
    return {
        "id": s.id,
        "slug": s.slug,
        "kind": s.kind,
        "label": s.label,
        "url": s.url,
        "config": s.config,
        "enabled": s.enabled,
        "default_priority": s.default_priority,
        "project_slug": s.project_slug,
        "poll_interval_seconds": s.poll_interval_seconds,
        "last_checked_at": s.last_checked_at,
        "last_status": s.last_status,
        "consecutive_failures": s.consecutive_failures,
        "mute_until": s.mute_until,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


def _topic_to_dict(t: Any) -> dict[str, Any]:
    return {
        "id": t.id,
        "slug": t.slug,
        "label": t.label,
        "description": t.description,
        "priority": t.priority,
        "project_slug": t.project_slug,
        "keywords": t.keywords,
        "mute_until": t.mute_until,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }


def _item_to_dict(i: Any) -> dict[str, Any]:
    return {
        "id": i.id,
        "source_id": i.source_id,
        "title": i.title,
        "url": i.url,
        "canonical_url": i.canonical_url,
        "external_id": i.external_id,
        "published_at": i.published_at,
        "fetched_at": i.fetched_at,
        "status": i.status,
        "summary": i.summary,
        "score": i.score,
        "score_reason": i.score_reason,
        "score_dimensions": i.score_dimensions,
        "brain_path": i.brain_path,
    }


# ── Sources ──────────────────────────────────────────────────────


class CreateSourceBody(BaseModel):
    slug: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2048)
    config: dict[str, Any] | None = None
    enabled: bool = True
    default_priority: str = "medium"
    project_slug: str | None = None
    poll_interval_seconds: int = 3600


class UpdateSourceBody(BaseModel):
    label: str | None = None
    url: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None
    default_priority: str | None = None
    project_slug: str | None = None
    poll_interval_seconds: int | None = None
    mute_until: str | None = None
    # Distinguish "don't touch" from "set to null" for the optional
    # nullable fields. The body uses an explicit ``clear_*`` flag for
    # each rather than smuggling a sentinel through pydantic.
    clear_project_slug: bool = False
    clear_mute_until: bool = False


@router.get("/sources")
async def list_sources(
    request: Request,
    enabled_only: bool = False,
    project_slug: str | None = None,
) -> dict:
    sources = await _sources(request).list_sources(
        enabled_only=enabled_only, project_slug=project_slug,
    )
    return {"sources": [_source_to_dict(s) for s in sources]}


@router.post("/sources")
async def create_source(
    body: CreateSourceBody, request: Request,
) -> dict:
    try:
        s = await _sources(request).create(
            slug=body.slug,
            kind=body.kind,  # type: ignore[arg-type]
            label=body.label,
            url=body.url,
            config=body.config,
            enabled=body.enabled,
            default_priority=body.default_priority,  # type: ignore[arg-type]
            project_slug=body.project_slug,
            poll_interval_seconds=body.poll_interval_seconds,
        )
    except SourceValidationError as e:
        raise HTTPException(400, str(e)) from e
    return _source_to_dict(s)


@router.get("/sources/{source_id}")
async def get_source(source_id: str, request: Request) -> dict:
    s = await _sources(request).get(source_id)
    if s is None:
        raise HTTPException(404, f"source {source_id} not found")
    return _source_to_dict(s)


@router.put("/sources/{source_id}")
async def update_source(
    source_id: str, body: UpdateSourceBody, request: Request,
) -> dict:
    try:
        kwargs: dict[str, Any] = {
            "label": body.label,
            "url": body.url,
            "config": body.config,
            "enabled": body.enabled,
            "default_priority": body.default_priority,
            "poll_interval_seconds": body.poll_interval_seconds,
        }
        if body.clear_project_slug:
            kwargs["project_slug"] = None
        elif body.project_slug is not None:
            kwargs["project_slug"] = body.project_slug
        if body.clear_mute_until:
            kwargs["mute_until"] = None
        elif body.mute_until is not None:
            kwargs["mute_until"] = body.mute_until
        s = await _sources(request).update(source_id, **kwargs)
    except SourceValidationError as e:
        raise HTTPException(400, str(e)) from e
    if s is None:
        raise HTTPException(404, f"source {source_id} not found")
    return _source_to_dict(s)


@router.delete("/sources/{source_id}")
async def delete_source(source_id: str, request: Request) -> dict:
    removed = await _sources(request).delete(source_id)
    if not removed:
        raise HTTPException(404, f"source {source_id} not found")
    return {"removed": True, "id": source_id}


@router.post("/sources/{source_id}/test")
async def test_source(source_id: str, request: Request) -> dict:
    """Preview-only fetch: pull the source, parse it, return up to
    10 items WITHOUT writing them to the DB. Lets the operator
    confirm a feed parses before committing to it."""
    s = await _sources(request).get(source_id)
    if s is None:
        raise HTTPException(404, f"source {source_id} not found")
    try:
        result = await fetch_for_source(s)
    except NotImplementedFetchError as e:
        raise HTTPException(501, str(e)) from e
    except FetchError as e:
        return {
            "ok": False,
            "message": str(e),
            "items": [],
        }
    preview = [
        {
            "title": item.title,
            "url": item.url,
            "published_at": item.published_at,
            "external_id": item.external_id,
            "body_chars": len(item.body),
        }
        for item in result.items[:10]
    ]
    return {
        "ok": True,
        "items_seen": len(result.items),
        "preview": preview,
        "etag": result.etag,
        "last_modified": result.last_modified,
        "note": result.note,
    }


@router.post("/sources/{source_id}/refresh")
async def refresh_source(
    source_id: str,
    request: Request,
    dry_run: bool = False,
) -> dict:
    """Real fetch: pull the source, dedupe, persist new items, score
    via the keyword scorer, and (when score clears the resolved
    threshold) write a markdown note under ``world/<date>/``.

    The threshold is resolved per-call:
      1. ``source.config['min_score']`` (per-source override, 0-100)
      2. ``PILK_INTELLIGENCE_BRAIN_THRESHOLD`` (global default)

    Set ``?dry_run=true`` to fetch + score + dedup-store WITHOUT
    creating any brain notes. The response then carries
    ``items_would_brain_write`` so the operator can preview what a
    given threshold change would land in the vault. Items still
    land in SQLite so dedup state stays accurate for future calls.

    Same pipeline the optional daemon uses — keeping them in lock-
    step means the manual path can never drift from the autonomous
    one. Does NOT alert, NOT call any LLM, NOT fire plans."""
    s = await _sources(request).get(source_id)
    if s is None:
        raise HTTPException(404, f"source {source_id} not found")

    pipeline = _pipeline(request)
    outcome = await pipeline.run_source(s, dry_run=dry_run)
    if (
        not outcome.ok
        and outcome.error
        and "hasn't been implemented" in outcome.error
    ):
        raise HTTPException(501, outcome.error)
    return {
        "ok": outcome.ok,
        "run_id": outcome.run_id,
        "dry_run": outcome.dry_run,
        "threshold_applied": outcome.threshold_applied,
        "threshold_source": outcome.threshold_source,
        "items_seen": outcome.items_seen,
        "items_new": outcome.items_new,
        "items_dup": outcome.items_dup,
        "items_brain_written": outcome.items_brain_written,
        "items_would_brain_write": outcome.items_would_brain_write,
        "note": outcome.note,
        "error": outcome.error,
    }


# ── Manual item submission (Batch 3C) ───────────────────────────


class SubmitManualItemBody(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=8000)
    published_at: str | None = Field(default=None, max_length=64)


@router.post("/sources/{source_id}/items")
async def submit_manual_item(
    source_id: str,
    body: SubmitManualItemBody,
    request: Request,
) -> dict:
    """Submit a single operator-curated item to a ``manual`` source.

    Required: a URL. Optional: title (auto-fetched from <title> if
    omitted, with a URL-derived fallback if fetch fails), notes
    (operator's free-form context), published_at (ISO 8601 date).

    The submitted item flows through the same dedup → keyword score
    → conditional brain-write pipeline as auto-fetched items, with
    one polite GET attempted at most for title extraction. Refused
    when the source's ``kind`` isn't ``manual``.

    No Telegram alert. No plan creation. No autonomous action. No
    LLM call. No vector embedding.
    """
    from core.config import get_settings

    s = await _sources(request).get(source_id)
    if s is None:
        raise HTTPException(404, f"source {source_id} not found")
    if s.kind != "manual":
        raise HTTPException(
            400,
            (
                f"source '{s.slug}' is kind '{s.kind}', not 'manual'. "
                "POST /items only accepts items for sources of kind "
                "'manual'. Use POST /sources/{id}/refresh for "
                "auto-fetched kinds (rss / hacker_news / "
                "github_releases / arxiv)."
            ),
        )

    settings = get_settings()
    outcome = await ingest_manual_item(
        source=s,
        url=body.url,
        title=body.title,
        notes=body.notes,
        published_at=body.published_at,
        topics=_topics(request),
        items=_items(request),
        brain=getattr(request.app.state, "brain", None),
        global_threshold=settings.intelligence_brain_write_threshold,
    )
    if not outcome.ok:
        raise HTTPException(400, outcome.error or "manual ingest failed")
    return {
        "ok": True,
        "items_seen": outcome.items_seen,
        "items_new": outcome.items_new,
        "items_dup": outcome.items_dup,
        "items_brain_written": outcome.items_brain_written,
        "item_id": outcome.item_id,
        "title_used": outcome.title_used,
        "title_source": outcome.title_source,
        "fetch_attempted": outcome.fetch_attempted,
        "fetch_succeeded": outcome.fetch_succeeded,
        "fetch_error": outcome.fetch_error,
        "score": outcome.score,
        "matched_topics": outcome.matched_topics or [],
        "threshold_applied": outcome.threshold_applied,
        "threshold_source": outcome.threshold_source,
        "brain_path": outcome.brain_path,
    }


# ── Topics ───────────────────────────────────────────────────────


class CreateTopicBody(BaseModel):
    slug: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    description: str = ""
    priority: str = "medium"
    project_slug: str | None = None
    keywords: list[str] = Field(default_factory=list)


class UpdateTopicBody(BaseModel):
    label: str | None = None
    description: str | None = None
    priority: str | None = None
    keywords: list[str] | None = None


@router.get("/topics")
async def list_topics(
    request: Request, project_slug: str | None = None,
) -> dict:
    topics = await _topics(request).list_topics(project_slug=project_slug)
    return {"topics": [_topic_to_dict(t) for t in topics]}


@router.post("/topics")
async def create_topic(body: CreateTopicBody, request: Request) -> dict:
    try:
        t = await _topics(request).create(
            slug=body.slug,
            label=body.label,
            description=body.description,
            priority=body.priority,  # type: ignore[arg-type]
            project_slug=body.project_slug,
            keywords=body.keywords,
        )
    except TopicValidationError as e:
        raise HTTPException(400, str(e)) from e
    return _topic_to_dict(t)


@router.get("/topics/{topic_id}")
async def get_topic(topic_id: str, request: Request) -> dict:
    t = await _topics(request).get(topic_id)
    if t is None:
        raise HTTPException(404, f"topic {topic_id} not found")
    return _topic_to_dict(t)


@router.put("/topics/{topic_id}")
async def update_topic(
    topic_id: str, body: UpdateTopicBody, request: Request,
) -> dict:
    try:
        t = await _topics(request).update(
            topic_id,
            label=body.label,
            description=body.description,
            priority=body.priority,  # type: ignore[arg-type]
            keywords=body.keywords,
        )
    except TopicValidationError as e:
        raise HTTPException(400, str(e)) from e
    if t is None:
        raise HTTPException(404, f"topic {topic_id} not found")
    return _topic_to_dict(t)


@router.delete("/topics/{topic_id}")
async def delete_topic(topic_id: str, request: Request) -> dict:
    removed = await _topics(request).delete(topic_id)
    if not removed:
        raise HTTPException(404, f"topic {topic_id} not found")
    return {"removed": True, "id": topic_id}


# ── Items + runs (read-only) ─────────────────────────────────────


@router.get("/items")
async def list_items(
    request: Request,
    source_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    items = await _items(request).list_items(
        source_id=source_id,
        status=status,  # type: ignore[arg-type]
        limit=limit,
        offset=offset,
    )
    return {"items": [_item_to_dict(i) for i in items]}


@router.get("/digest")
async def get_digest(
    request: Request,
    since: str | None = None,
    project: str | None = None,
    include_global: bool = False,
    source: str | None = None,
    topic: str | None = None,
    min_score: int | None = None,
    limit: int = 50,
) -> dict:
    """Operator-pulled digest of recent intelligence items.

    Read-only. No file writes, no LLM calls, no Telegram pushes, no
    plan creation. Designed to power the future Master Reporting
    "what's new in the world?" brief without enabling any
    autonomy.

    Query params:
      - ``since`` (optional, ISO 8601): only items fetched at or after
        this timestamp. Partial dates like ``2026-04-27`` work.
      - ``project`` (optional): only items from sources with this
        ``project_slug``.
      - ``include_global`` (optional, default false): when ``project``
        is set, also include items from sources with NO project_slug.
      - ``source`` (optional): filter by source slug.
      - ``topic`` (optional): filter to items that matched this topic
        slug during keyword scoring.
      - ``min_score`` (optional, 0-100): items with at least this
        relevance score.
      - ``limit`` (default 50, max 200).

    Response shape: a JSON object with ``items`` (newest-first list
    of digest entries) and the resolved filter parameters echoed
    back so a caller can confirm what was applied.
    """
    items = _items(request)
    entries = await items.digest(
        since=since,
        project=project,
        include_global=include_global,
        source_slug=source,
        topic=topic,
        min_score=min_score,
        limit=limit,
    )
    capped_limit = max(1, min(int(limit), 200))
    return {
        "filters": {
            "since": since,
            "project": project,
            "include_global": include_global,
            "source": source,
            "topic": topic,
            "min_score": min_score,
            "limit": capped_limit,
        },
        "count": len(entries),
        "items": [
            {
                "item_id": e.item_id,
                "title": e.title,
                "url": e.url,
                "source_slug": e.source_slug,
                "source_label": e.source_label,
                "source_kind": e.source_kind,
                "project_slug": e.project_slug,
                "published_at": e.published_at,
                "fetched_at": e.fetched_at,
                "score": e.score,
                "score_reason": e.score_reason,
                "brain_path": e.brain_path,
                "status": e.status,
                "matched_topics": e.matched_topics,
            }
            for e in entries
        ],
    }


@router.get("/runs")
async def list_runs(
    request: Request, source_id: str | None = None, limit: int = 20,
) -> dict:
    runs = await _items(request).recent_runs(
        source_id=source_id, limit=limit,
    )
    return {
        "runs": [
            {
                "id": r.run_id,
                "source_id": r.source_id,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "status": r.status,
                "items_seen": r.items_seen,
                "items_new": r.items_new,
                "items_dup": r.items_dup,
                "error": r.error,
            }
            for r in runs
        ]
    }
