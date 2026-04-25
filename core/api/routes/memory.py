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
import os
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
DISTILL_MAX_PROPOSALS = 8          # hard cap on returned proposals
DISTILL_MAX_GOAL_CHARS = 400       # truncate each goal for context
DISTILL_CANDIDATE_COUNT = 12       # GRPO: ask the generator for this many
DISTILL_KEEP_COUNT = 5             # GRPO: keep top-N after judge ranks
DISTILL_GRPO_ENV = "PILK_DISTILL_GRPO"

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
    "- Cast a wide net: propose up to 12 candidates spanning all four "
    "kinds when the evidence allows. A downstream judge ranks and "
    "filters — don't pre-censor here, that's the judge's job.\n"
    "- If there's nothing durable to extract, return "
    '{"proposals": []}.\n'
)

DISTILL_JUDGE_SYSTEM_PROMPT = (
    "You are PILK's memory curator. You receive a list of candidate "
    "memory entries proposed by another model from the same evidence. "
    "Your job is to rank them group-relatively on four axes:\n\n"
    "- durability: will this still matter in a month?\n"
    "- actionability: can PILK change behavior next turn because of "
    "this?\n"
    "- generality: does it capture a pattern, not a one-off?\n"
    "- non_redundancy: does it add something the other candidates "
    "don't already cover?\n\n"
    "Output JSON ONLY, no prose. Shape:\n"
    '{"rankings": [{"index": <0-based int>, "durability": 0.0-1.0, '
    '"actionability": 0.0-1.0, "generality": 0.0-1.0, '
    '"non_redundancy": 0.0-1.0, "verdict": "keep|drop", '
    '"reason": "one short sentence"}]}\n\n'
    "Rules:\n"
    "- Score relatively against the OTHER candidates, not in absolute "
    "terms. The point is to surface the strongest of THIS group.\n"
    "- Mark verdict 'drop' for redundant or weak entries even if "
    "they're individually fine — the operator only wants the cream.\n"
    "- Include EVERY candidate exactly once, identified by its "
    "0-based index in the list provided.\n"
)


def _grpo_enabled() -> bool:
    """Default ON. Set ``PILK_DISTILL_GRPO=0`` to fall back to the
    single-call path."""
    raw = os.getenv(DISTILL_GRPO_ENV, "1").strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


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
            max_tokens=1500,
            system=DISTILL_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as e:
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
    candidates = _sanitize_proposals(payload.get("proposals"))

    log.info(
        "distill_extracted",
        window=window,
        candidate_count=len(candidates),
    )

    proposals = candidates
    if _grpo_enabled() and len(candidates) > 1:
        ranked = await _rank_candidates(client, candidates)
        if ranked is None:
            log.info("distill_judge_fallback", reason="rank_unavailable")
        else:
            kept = ranked[:DISTILL_KEEP_COUNT]
            log.info(
                "distill_judged",
                total=len(candidates),
                kept=len(kept),
                dropped=max(0, len(ranked) - len(kept)),
            )
            proposals = kept

    return {
        "proposals": proposals[:DISTILL_MAX_PROPOSALS],
        "window": len(plans),
    }


async def _rank_candidates(
    client: Any, candidates: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Run the judge pass: ask Haiku to score the candidates relative
    to one another and return them sorted high-to-low with a ``score``
    field attached. Returns ``None`` on any failure so the caller can
    fall back to the unranked list."""
    listing = "\n".join(
        f"[{i}] kind={c['kind']} title={c['title']!r} body={c['body']!r}"
        for i, c in enumerate(candidates)
    )
    user_content = (
        f"Rank these {len(candidates)} candidate memory entries:\n\n"
        f"{listing}"
    )
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1500,
            system=DISTILL_JUDGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception:
        log.exception("distill_judge_failed")
        return None
    text = ""
    for block in resp.content or []:
        if getattr(block, "type", None) == "text":
            text += getattr(block, "text", "")
    rankings = _parse_judge_json(text)
    if not rankings:
        return None
    scored: list[tuple[float, dict[str, Any]]] = []
    seen: set[int] = set()
    for rank in rankings:
        idx = rank.get("index") if isinstance(rank, dict) else None
        if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
            continue
        if idx in seen:
            continue
        seen.add(idx)
        verdict = str(rank.get("verdict") or "").strip().lower()
        if verdict == "drop":
            score = 0.0
        else:
            axes = [
                rank.get("durability"),
                rank.get("actionability"),
                rank.get("generality"),
                rank.get("non_redundancy"),
            ]
            nums = [float(a) for a in axes if isinstance(a, (int, float))]
            score = sum(nums) / len(nums) if nums else 0.0
        cand = dict(candidates[idx])
        cand["score"] = score
        scored.append((score, cand))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored]


def _parse_judge_json(text: str) -> list[dict[str, Any]]:
    """Extract the ``rankings`` list from the judge's JSON response.
    Returns ``[]`` on any parse problem so the caller falls back."""
    body = text.strip()
    if body.startswith("```"):
        first_nl = body.find("\n")
        if first_nl > 0:
            body = body[first_nl + 1 :]
        if body.endswith("```"):
            body = body[:-3]
        body = body.strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.warning("distill_judge_non_json", body_prefix=body[:120])
        return []
    if not isinstance(data, dict):
        return []
    rankings = data.get("rankings")
    return rankings if isinstance(rankings, list) else []


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
