"""Persistence for workflow runs + steps."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.db import connect


def _uid(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class WorkflowRunStep:
    id: str
    run_id: str
    idx: int
    name: str
    kind: str
    status: str
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class WorkflowRun:
    id: str
    workflow_name: str
    status: str
    inputs: dict[str, Any] = field(default_factory=dict)
    current_step: int = 0
    checkpoint: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    finished_at: str | None = None
    steps: list[WorkflowRunStep] = field(default_factory=list)


class WorkflowStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    async def create_run(
        self,
        *,
        workflow_name: str,
        inputs: dict[str, Any],
    ) -> WorkflowRun:
        rid = _uid("wfr")
        now = _now()
        async with connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO workflow_runs("
                "id, workflow_name, status, inputs_json, "
                "current_step, checkpoint_json, created_at, updated_at) "
                "VALUES(?, ?, 'pending', ?, 0, '{}', ?, ?)",
                (rid, workflow_name, json.dumps(inputs), now, now),
            )
            await conn.commit()
        return WorkflowRun(
            id=rid, workflow_name=workflow_name, status="pending",
            inputs=inputs, created_at=now, updated_at=now,
        )

    async def add_step(
        self,
        *,
        run_id: str,
        idx: int,
        name: str,
        kind: str,
        input_data: dict[str, Any] | None = None,
    ) -> WorkflowRunStep:
        sid = _uid("wfs")
        now = _now()
        async with connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO workflow_steps("
                "id, run_id, idx, name, kind, status, input_json, "
                "started_at) VALUES(?, ?, ?, ?, ?, 'running', ?, ?)",
                (
                    sid, run_id, idx, name, kind,
                    json.dumps(input_data or {}), now,
                ),
            )
            await conn.commit()
        return WorkflowRunStep(
            id=sid, run_id=run_id, idx=idx, name=name, kind=kind,
            status="running", input=input_data or {},
            started_at=now,
        )

    async def finish_step(
        self,
        step_id: str,
        *,
        status: str,
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = _now()
        async with connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE workflow_steps SET status = ?, output_json = ?, "
                "error = ?, finished_at = ? WHERE id = ?",
                (
                    status, json.dumps(output or {}), error, now, step_id,
                ),
            )
            await conn.commit()

    async def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        current_step: int | None = None,
        checkpoint: dict[str, Any] | None = None,
        error: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if current_step is not None:
            sets.append("current_step = ?")
            params.append(int(current_step))
        if checkpoint is not None:
            sets.append("checkpoint_json = ?")
            params.append(json.dumps(checkpoint))
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if finished_at is not None:
            sets.append("finished_at = ?")
            params.append(finished_at)
        sets.append("updated_at = ?")
        params.append(_now())
        params.append(run_id)
        sql = (
            "UPDATE workflow_runs SET "
            + ", ".join(sets)
            + " WHERE id = ?"
        )
        async with connect(self.db_path) as conn:
            await conn.execute(sql, params)
            await conn.commit()

    async def get_run(self, run_id: str) -> WorkflowRun | None:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT id, workflow_name, status, inputs_json, "
                "current_step, checkpoint_json, error, created_at, "
                "updated_at, finished_at FROM workflow_runs "
                "WHERE id = ?",
                (run_id,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            run = _hydrate_run(row)
            async with conn.execute(
                "SELECT id, run_id, idx, name, kind, status, "
                "input_json, output_json, error, started_at, "
                "finished_at FROM workflow_steps "
                "WHERE run_id = ? ORDER BY idx ASC",
                (run_id,),
            ) as cur:
                step_rows = await cur.fetchall()
        run.steps = [_hydrate_step(r) for r in step_rows]
        return run

    async def list_runs(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
        workflow_name: str | None = None,
    ) -> list[WorkflowRun]:
        wheres: list[str] = []
        params: list[Any] = []
        if status is not None:
            wheres.append("status = ?")
            params.append(status)
        if workflow_name is not None:
            wheres.append("workflow_name = ?")
            params.append(workflow_name)
        where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
        params.append(int(limit))
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT id, workflow_name, status, inputs_json, "
                "current_step, checkpoint_json, error, created_at, "
                "updated_at, finished_at FROM workflow_runs"
                + where_sql
                + " ORDER BY created_at DESC LIMIT ?",
                params,
            ) as cur:
                rows = await cur.fetchall()
        return [_hydrate_run(r) for r in rows]


def _hydrate_run(row: Any) -> WorkflowRun:
    try:
        inputs = json.loads(row[3] or "{}")
    except json.JSONDecodeError:
        inputs = {}
    try:
        ckpt = json.loads(row[5] or "{}")
    except json.JSONDecodeError:
        ckpt = {}
    return WorkflowRun(
        id=row[0],
        workflow_name=row[1],
        status=row[2],
        inputs=inputs,
        current_step=int(row[4] or 0),
        checkpoint=ckpt,
        error=row[6],
        created_at=row[7] or "",
        updated_at=row[8] or "",
        finished_at=row[9],
    )


def _hydrate_step(row: Any) -> WorkflowRunStep:
    try:
        input_data = json.loads(row[6] or "{}")
    except json.JSONDecodeError:
        input_data = {}
    try:
        output = json.loads(row[7] or "{}")
    except json.JSONDecodeError:
        output = {}
    return WorkflowRunStep(
        id=row[0],
        run_id=row[1],
        idx=int(row[2] or 0),
        name=row[3],
        kind=row[4],
        status=row[5],
        input=input_data,
        output=output,
        error=row[8],
        started_at=row[9],
        finished_at=row[10],
    )


__all__ = ["WorkflowRun", "WorkflowRunStep", "WorkflowStore"]
