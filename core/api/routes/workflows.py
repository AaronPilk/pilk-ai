"""HTTP surface for the workflow engine.

  GET   /workflows                    list registered workflows
  GET   /workflows/{name}              one workflow's manifest
  POST  /workflows/{name}/run          start a new run
  GET   /workflows/runs                list recent runs
  GET   /workflows/runs/{id}           one run with steps
  POST  /workflows/runs/{id}/resume    resume a paused run
  POST  /workflows/runs/{id}/cancel    cancel a running/paused run
  POST  /workflows/reload              reload manifests from disk

All run-mutating routes are operator-pulled. No workflow auto-fires
in this batch — ``trigger: cron`` and ``trigger: event`` in a
manifest are documented for a later batch.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.workflows import (
    Workflow,
    WorkflowEngine,
    WorkflowExecutionError,
    WorkflowRun,
    WorkflowStore,
)
from core.workflows.manifest import load_all_workflows

router = APIRouter(prefix="/workflows")


def _engine(request: Request) -> WorkflowEngine:
    e = getattr(request.app.state, "workflow_engine", None)
    if e is None:
        raise HTTPException(503, "workflow engine not initialised")
    return e


def _store(request: Request) -> WorkflowStore:
    s = getattr(request.app.state, "workflow_store", None)
    if s is None:
        raise HTTPException(503, "workflow store not initialised")
    return s


def _registry(request: Request) -> dict[str, Workflow]:
    r = getattr(request.app.state, "workflow_registry", None)
    if r is None:
        raise HTTPException(503, "workflow registry not initialised")
    return r


def _wf_to_dict(wf: Workflow) -> dict[str, Any]:
    return {
        "name": wf.name,
        "description": wf.description,
        "trigger": wf.trigger,
        "inputs": [
            {
                "name": i.name,
                "description": i.description,
                "required": i.required,
                "default": i.default,
            }
            for i in wf.inputs
        ],
        "steps": [
            {
                "name": s.name,
                "kind": s.kind,
                "description": s.description,
                "tool": s.tool,
                "agent": s.agent,
            }
            for s in wf.steps
        ],
        "manifest_path": wf.manifest_path,
    }


def _run_to_dict(run: WorkflowRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "workflow_name": run.workflow_name,
        "status": run.status,
        "inputs": run.inputs,
        "current_step": run.current_step,
        "checkpoint": run.checkpoint,
        "error": run.error,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "finished_at": run.finished_at,
        "steps": [
            {
                "id": s.id, "idx": s.idx, "name": s.name,
                "kind": s.kind, "status": s.status,
                "input": s.input, "output": s.output,
                "error": s.error,
                "started_at": s.started_at,
                "finished_at": s.finished_at,
            }
            for s in run.steps
        ],
    }


@router.get("")
async def list_workflows(request: Request) -> dict[str, Any]:
    reg = _registry(request)
    return {
        "workflows": [_wf_to_dict(w) for w in reg.values()],
        "count": len(reg),
    }


@router.get("/runs")
async def list_runs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
    workflow_name: str | None = Query(default=None),
) -> dict[str, Any]:
    runs = await _store(request).list_runs(
        limit=limit, status=status, workflow_name=workflow_name,
    )
    return {
        "runs": [_run_to_dict(r) for r in runs],
        "count": len(runs),
    }


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: str) -> dict[str, Any]:
    run = await _store(request).get_run(run_id)
    if run is None:
        raise HTTPException(404, "not found")
    return _run_to_dict(run)


class _RunRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)


@router.post("/{name}/run")
async def start_run(
    request: Request, name: str, payload: _RunRequest,
) -> dict[str, Any]:
    reg = _registry(request)
    wf = reg.get(name)
    if wf is None:
        raise HTTPException(404, f"workflow {name!r} not found")
    engine = _engine(request)
    try:
        run = await engine.start(wf, inputs=payload.inputs)
    except WorkflowExecutionError as e:
        raise HTTPException(400, str(e)) from e
    return _run_to_dict(run)


@router.post("/runs/{run_id}/resume")
async def resume_run(request: Request, run_id: str) -> dict[str, Any]:
    store = _store(request)
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "not found")
    reg = _registry(request)
    wf = reg.get(run.workflow_name)
    if wf is None:
        raise HTTPException(
            400,
            f"workflow {run.workflow_name!r} no longer registered; "
            f"cannot resume",
        )
    try:
        run = await _engine(request).resume(run_id, wf)
    except WorkflowExecutionError as e:
        raise HTTPException(400, str(e)) from e
    return _run_to_dict(run)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(request: Request, run_id: str) -> dict[str, Any]:
    await _engine(request).cancel(run_id)
    run = await _store(request).get_run(run_id)
    if run is None:
        raise HTTPException(404, "not found")
    return _run_to_dict(run)


@router.post("/reload")
async def reload_workflows(request: Request) -> dict[str, Any]:
    """Re-scan the workflows/ directory for new or changed manifests."""
    root = getattr(request.app.state, "workflow_root", None)
    if root is None:
        raise HTTPException(503, "workflow root not configured")
    workflows = load_all_workflows(root)
    request.app.state.workflow_registry = {
        w.name: w for w in workflows
    }
    return {"loaded": [w.name for w in workflows]}


@router.get("/{name}")
async def get_workflow(request: Request, name: str) -> dict[str, Any]:
    reg = _registry(request)
    wf = reg.get(name)
    if wf is None:
        raise HTTPException(404, f"workflow {name!r} not found")
    return _wf_to_dict(wf)
