"""CRUD for plans and steps.

Kept deliberately small — no ORM, no query builder. Each method issues
one or two SQL statements, commits, and returns a dict shaped like the
WebSocket event payload. Callers (the orchestrator) broadcast whatever
we return without reshaping.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.db import connect


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class PlanStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def create_plan(
        self,
        goal: str,
        *,
        metadata: dict[str, Any] | None = None,
        estimated_usd: float | None = None,
    ) -> dict[str, Any]:
        pid = _uid("plan")
        now = _now()
        meta_json = json.dumps(metadata) if metadata else None
        async with connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO plans(id, goal, status, created_at, updated_at, "
                "estimated_usd, metadata_json) VALUES (?, ?, 'running', ?, ?, ?, ?)",
                (pid, goal, now, now, estimated_usd, meta_json),
            )
            await conn.commit()
        return {
            "id": pid,
            "goal": goal,
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "actual_usd": 0.0,
            "estimated_usd": estimated_usd,
            "metadata": metadata or {},
            "agent_name": (metadata or {}).get("agent_name"),
        }

    async def set_estimated_usd(self, plan_id: str, estimated_usd: float) -> dict[str, Any]:
        now = _now()
        async with connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE plans SET estimated_usd = ?, updated_at = ? WHERE id = ?",
                (float(estimated_usd), now, plan_id),
            )
            await conn.commit()
        return await self.get_plan(plan_id)

    async def finish_plan(self, plan_id: str, status: str) -> dict[str, Any]:
        now = _now()
        async with connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE plans SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, plan_id),
            )
            await conn.commit()
        return await self.get_plan(plan_id)

    async def recover_orphaned_plans(self) -> list[str]:
        """Mark plans left mid-run by a previous daemon process as
        ``failed``. Called once at app startup.

        A plan whose ``status`` is one of {running, pending, paused}
        when the daemon boots was orphaned by a crash, kill, or
        restart — there is no live executor for it and it will never
        progress. Leaving these in the DB makes the dashboard
        misleading ('something is running!') and corrupts the
        ``stuck_task`` Sentinel rule's signal. We mark them failed
        with a clear error so the operator can see what happened.

        Steps belonging to those plans get the same treatment for
        consistency (a step in 'running' is never live across a
        daemon restart).

        Returns the list of plan IDs that were recovered.
        """
        now = _now()
        recovered: list[str] = []
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT id FROM plans "
                "WHERE status IN ('running', 'pending', 'paused')"
            ) as cur:
                rows = await cur.fetchall()
            for r in rows:
                recovered.append(r["id"])
            if recovered:
                await conn.execute(
                    "UPDATE plans SET status = 'failed', updated_at = ? "
                    "WHERE status IN ('running', 'pending', 'paused')",
                    (now,),
                )
                # Same for any non-terminal steps under those plans.
                await conn.execute(
                    "UPDATE steps SET status = 'failed', "
                    "finished_at = COALESCE(finished_at, ?), "
                    "error = COALESCE(error, "
                    "'orphaned: daemon restarted before completion') "
                    "WHERE status IN ('pending', 'running', "
                    "'awaiting_approval')",
                    (now,),
                )
                await conn.commit()
        return recovered

    async def get_plan(self, plan_id: str) -> dict[str, Any]:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT id, goal, status, created_at, updated_at, "
                "estimated_usd, actual_usd, metadata_json FROM plans WHERE id = ?",
                (plan_id,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise LookupError(f"plan {plan_id} not found")
            plan = _hydrate_plan(dict(row))
            async with conn.execute(
                "SELECT id, plan_id, idx, kind, description, status, risk_class, "
                "input_json, output_json, started_at, finished_at, cost_usd, error "
                "FROM steps WHERE plan_id = ? ORDER BY idx ASC",
                (plan_id,),
            ) as cur:
                steps = [dict(r) for r in await cur.fetchall()]
        plan["steps"] = [_hydrate_step(s) for s in steps]
        return plan

    async def list_plans(self, limit: int = 50) -> list[dict[str, Any]]:
        async with connect(self.db_path) as conn, conn.execute(
            "SELECT id, goal, status, created_at, updated_at, "
            "estimated_usd, actual_usd, metadata_json FROM plans "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [_hydrate_plan(dict(r)) for r in rows]

    async def add_step(
        self,
        *,
        plan_id: str,
        kind: str,
        description: str,
        risk_class: str,
        input_data: dict | None = None,
    ) -> dict[str, Any]:
        sid = _uid("step")
        now = _now()
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT COALESCE(MAX(idx), -1) + 1 FROM steps WHERE plan_id = ?",
                (plan_id,),
            ) as cur:
                idx = (await cur.fetchone())[0]
            await conn.execute(
                "INSERT INTO steps(id, plan_id, idx, kind, description, status, "
                "risk_class, input_json, started_at) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)",
                (
                    sid,
                    plan_id,
                    idx,
                    kind,
                    description,
                    risk_class,
                    json.dumps(input_data) if input_data is not None else None,
                    now,
                ),
            )
            await conn.commit()
        return _hydrate_step(
            {
                "id": sid,
                "plan_id": plan_id,
                "idx": idx,
                "kind": kind,
                "description": description,
                "status": "running",
                "risk_class": risk_class,
                "input_json": json.dumps(input_data) if input_data is not None else None,
                "output_json": None,
                "started_at": now,
                "finished_at": None,
                "cost_usd": 0.0,
                "error": None,
            }
        )

    async def set_step_status(
        self, step_id: str, *, status: str
    ) -> dict[str, Any]:
        """Change a step's status without finishing it.

        Used when the gateway pauses a step for approval — we flip to
        `awaiting_approval` and back to `running` without touching
        started_at / finished_at.
        """
        async with connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE steps SET status = ? WHERE id = ?", (status, step_id)
            )
            await conn.commit()
            async with conn.execute(
                "SELECT id, plan_id, idx, kind, description, status, risk_class, "
                "input_json, output_json, started_at, finished_at, cost_usd, error "
                "FROM steps WHERE id = ?",
                (step_id,),
            ) as cur:
                row = await cur.fetchone()
        return _hydrate_step(dict(row))

    async def finish_step(
        self,
        step_id: str,
        *,
        status: str,
        output: dict | None = None,
        cost_usd: float = 0.0,
        error: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        async with connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE steps SET status = ?, output_json = ?, finished_at = ?, "
                "cost_usd = COALESCE(cost_usd, 0) + ?, error = ? WHERE id = ?",
                (
                    status,
                    json.dumps(output) if output is not None else None,
                    now,
                    cost_usd,
                    error,
                    step_id,
                ),
            )
            await conn.commit()
            async with conn.execute(
                "SELECT id, plan_id, idx, kind, description, status, risk_class, "
                "input_json, output_json, started_at, finished_at, cost_usd, error "
                "FROM steps WHERE id = ?",
                (step_id,),
            ) as cur:
                row = await cur.fetchone()
        return _hydrate_step(dict(row))


def _hydrate_step(row: dict) -> dict:
    """Parse JSON blobs so the client receives structured data."""
    out = dict(row)
    for key in ("input_json", "output_json"):
        val = out.get(key)
        if isinstance(val, str):
            try:
                out[key.removesuffix("_json")] = json.loads(val)
            except json.JSONDecodeError:
                out[key.removesuffix("_json")] = val
            out.pop(key, None)
        else:
            out[key.removesuffix("_json")] = None
            out.pop(key, None)
    return out


def _hydrate_plan(row: dict) -> dict:
    out = dict(row)
    raw = out.pop("metadata_json", None)
    meta: dict[str, Any] = {}
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                meta = parsed
        except json.JSONDecodeError:
            pass
    out["metadata"] = meta
    out["agent_name"] = meta.get("agent_name")
    return out
