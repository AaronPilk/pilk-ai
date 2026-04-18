"""Operational timeline — plain-English rows composed from existing tables.

  GET /logs?kind=&q=&limit=&before=

This is a *read-only aggregator*. Nothing here writes, nothing here
introduces a new log pipeline. We read from the tables we already
persist (plans, approvals, trust_audit) and hand the UI one merged
chronological feed. The UI is responsible for rendering rows as
sentences — the payload stays structured but human-friendly.

Scope floor (batch J):
- Three row kinds: plan, approval, trust.
- Text search is applied client-side; we just return the latest
  window filtered by kind + pagination cursor.
- No new WS channel; the existing plan.* / approval.* events already
  tell the UI to append rows live.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from core.db import connect

router = APIRouter(prefix="/logs")

VALID_KINDS: frozenset[str] = frozenset({"plan", "approval", "trust"})
MAX_LIMIT = 200
DEFAULT_LIMIT = 50


@router.get("")
async def list_logs(
    request: Request,
    kind: str | None = None,
    limit: int = DEFAULT_LIMIT,
    before: str | None = None,
) -> dict:
    if kind is not None and kind not in VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown kind: {kind}")
    limit = max(1, min(limit, MAX_LIMIT))
    db = request.app.state.plans.db_path  # reuse plan store's db path

    rows: list[dict] = []
    async with connect(db) as conn:
        if kind in (None, "plan"):
            rows.extend(await _plan_rows(conn, limit, before))
        if kind in (None, "approval"):
            rows.extend(await _approval_rows(conn, limit, before))
        if kind in (None, "trust"):
            rows.extend(await _trust_rows(conn, limit, before))

    # Merge and trim. Rows are already time-sorted within each bucket;
    # one final sort keeps the feed stable across buckets.
    rows.sort(key=lambda r: r["at"], reverse=True)
    trimmed = rows[:limit]
    next_cursor = trimmed[-1]["at"] if len(trimmed) == limit else None
    return {"entries": trimmed, "next_before": next_cursor}


async def _plan_rows(conn, limit: int, before: str | None) -> list[dict]:
    sql = (
        "SELECT id, goal, status, created_at, updated_at, actual_usd "
        "FROM plans"
    )
    args: tuple = ()
    if before:
        sql += " WHERE datetime(created_at) < datetime(?)"
        args = (before,)
    sql += " ORDER BY datetime(created_at) DESC LIMIT ?"
    args = (*args, limit)
    async with conn.execute(sql, args) as cur:
        rows = await cur.fetchall()
    return [
        {
            "kind": "plan",
            "id": f"plan:{r['id']}",
            "at": r["created_at"],
            "title": r["goal"],
            "status": r["status"],
            "cost_usd": float(r["actual_usd"] or 0.0),
            "plan_id": r["id"],
        }
        for r in rows
    ]


async def _approval_rows(conn, limit: int, before: str | None) -> list[dict]:
    sql = (
        "SELECT id, plan_id, tool, risk_class, status, decided_at, "
        "created_at, decision_reason FROM approvals"
    )
    args: tuple = ()
    if before:
        sql += " WHERE datetime(COALESCE(decided_at, created_at)) < datetime(?)"
        args = (before,)
    sql += " ORDER BY datetime(COALESCE(decided_at, created_at)) DESC LIMIT ?"
    args = (*args, limit)
    async with conn.execute(sql, args) as cur:
        rows = await cur.fetchall()
    return [
        {
            "kind": "approval",
            "id": f"approval:{r['id']}",
            "at": r["decided_at"] or r["created_at"],
            "title": r["tool"],                  # UI humanizes tool → English
            "status": r["status"],               # pending|approved|rejected|expired
            "risk_class": r["risk_class"],
            "reason": r["decision_reason"] or "",
            "plan_id": r["plan_id"],
        }
        for r in rows
    ]


async def _trust_rows(conn, limit: int, before: str | None) -> list[dict]:
    sql = (
        "SELECT id, tool_name, agent_name, ttl_seconds, expires_at, "
        "created_at, reason FROM trust_audit"
    )
    args: tuple = ()
    if before:
        sql += " WHERE datetime(created_at) < datetime(?)"
        args = (before,)
    sql += " ORDER BY datetime(created_at) DESC LIMIT ?"
    args = (*args, limit)
    async with conn.execute(sql, args) as cur:
        rows = await cur.fetchall()
    return [
        {
            "kind": "trust",
            "id": f"trust:{r['id']}",
            "at": r["created_at"],
            "title": r["tool_name"],
            "agent_name": r["agent_name"],
            "ttl_seconds": int(r["ttl_seconds"] or 0),
            "expires_at": r["expires_at"],
            "reason": r["reason"] or "",
        }
        for r in rows
    ]
