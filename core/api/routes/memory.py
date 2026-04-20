"""HTTP surface for the structured memory store.

  GET    /memory                list entries, optional ?kind=
  POST   /memory                body: {kind, title, body?} → add
  DELETE /memory/{id}           delete one entry
  DELETE /memory                clear all (or ?kind=…)
  POST   /memory/distill        analyze recent conversations and
                                return proposed memory entries for
                                the operator to approve/skip. No
                                writes happen here — the client calls
                                POST /memory per approved entry.

All writes broadcast on the existing hub so other dashboards re-hydrate.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.logging import get_logger
from core.memory import MemoryKind, MemoryStore

log = get_logger("pilkd.memory.route")

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


# ── Auto-learning (distill) ─────────────────────────────────────

DISTILL_DEFAULT_WINDOW = 30        # pull the last N plans
DISTILL_MAX_PROPOSALS = 8          # cap what we ask Haiku to return
DISTILL_MAX_GOAL_CHARS = 400       # truncate each goal for context

DISTILL_SYSTEM_PROMPT = (
    "You are PILK's self-improvement extractor. You are given a list "
    "of recent user goals the operator has sent and one-line summaries "
    "of how the plan ended. Your job is to infer durable things about "
    "the operator — preferences, standing rules, facts, or recurring "
    "patterns — that PILK should remember to do its job better next "
    "time.\n\n"
    "Output JSON ONLY, no prose. Shape:\n"
    '{"proposals": [{"kind": "preference|standing_instruction|fact|'
    'pattern", "title": "short headline", "body": "why this is worth '
    'remembering", "confidence": 0.0-1.0, "rationale": "one sentence '
    'tying this to the evidence"}]}\n\n'
    "Rules:\n"
    "- Be conservative. Do NOT invent traits. Only propose things the "
    "evidence clearly supports.\n"
    "- Skip one-off ephemera ('asked about the weather once'). Focus "
    "on signals that repeat or reveal working style.\n"
    "- Keep titles short (≤ 80 chars) and bodies tight (≤ 300).\n"
    "- Max 5 proposals in a single batch. Fewer is fine.\n"
    "- If there's nothing durable to extract, return "
    '{"proposals": []}.\n'
)


class DistillBody(BaseModel):
    window: int = Field(
        default=DISTILL_DEFAULT_WINDOW,
        ge=5,
        le=100,
        description="How many recent plans to analyze.",
    )


@router.post("/distill")
async def distill_from_conversations(
    request: Request, body: DistillBody | None = None
) -> dict[str, Any]:
    """Ask Haiku to extract candidate memory entries from recent plan
    history. Returns a list of proposals the operator can then approve
    one-by-one via the existing POST /memory. No writes happen here."""
    plans_store = getattr(request.app.state, "plans", None)
    client = getattr(request.app.state, "anthropic", None)
    if plans_store is None or client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "distill requires plans + anthropic; check /system/status"
            ),
        )
    window = (body or DistillBody()).window
    plans = await plans_store.list_plans(limit=window)

    # Build a tight context — just the goal + status + final cost. We
    # deliberately don't dump full step logs because they're noisy and
    # expensive; the goal is enough signal to extract style + prefs.
    lines: list[str] = []
    for p in plans:
        goal = str(p.get("goal") or "").strip().replace("\n", " ")
        if len(goal) > DISTILL_MAX_GOAL_CHARS:
            goal = goal[: DISTILL_MAX_GOAL_CHARS - 1] + "…"
        status = p.get("status") or "unknown"
        lines.append(f"[{status}] {goal}")
    if not lines:
        return {"proposals": [], "window": 0}

    user_content = (
        f"Last {len(lines)} goals the operator sent to PILK "
        "(most recent first):\n\n" + "\n".join(lines)
    )

    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1200,
            system=DISTILL_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as e:  # noqa: BLE001
        log.exception("distill_anthropic_failed")
        raise HTTPException(
            status_code=502,
            detail=f"distill LLM call failed: {type(e).__name__}: {e}",
        ) from e

    text = ""
    for block in resp.content or []:
        if getattr(block, "type", None) == "text":
            text += getattr(block, "text", "")
    payload = _parse_distill_json(text)
    proposals = _sanitize_proposals(payload.get("proposals"))

    log.info(
        "distill_extracted",
        window=window,
        proposal_count=len(proposals),
    )
    return {
        "proposals": proposals[:DISTILL_MAX_PROPOSALS],
        "window": len(plans),
    }


def _parse_distill_json(text: str) -> dict:
    """Accept either raw JSON or JSON wrapped in a ```json ``` fence."""
    body = text.strip()
    if body.startswith("```"):
        # Strip a leading ``` / ```json and trailing ```.
        first_nl = body.find("\n")
        if first_nl > 0:
            body = body[first_nl + 1 :]
        if body.endswith("```"):
            body = body[:-3]
        body = body.strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.warning("distill_non_json_response", body_prefix=body[:120])
        return {"proposals": []}
    return data if isinstance(data, dict) else {"proposals": []}


def _sanitize_proposals(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    valid_kinds = {k.value for k in MemoryKind}
    out: list[dict[str, Any]] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        kind = str(r.get("kind") or "").strip().lower()
        title = str(r.get("title") or "").strip()
        if kind not in valid_kinds or not title:
            continue
        body = str(r.get("body") or "").strip()
        conf = r.get("confidence")
        try:
            conf_f = float(conf) if conf is not None else 0.5
        except (TypeError, ValueError):
            conf_f = 0.5
        conf_f = max(0.0, min(1.0, conf_f))
        out.append(
            {
                "kind": kind,
                "title": title[:140],
                "body": body[:1000],
                "confidence": conf_f,
                "rationale": str(r.get("rationale") or "").strip()[:280],
            }
        )
    return out
