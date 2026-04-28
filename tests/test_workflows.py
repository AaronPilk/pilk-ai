"""Phase 6 — workflow engine tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.brain import Vault
from core.db.migrations import ensure_schema
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome, ToolRegistry
from core.workflows import (
    Workflow,
    WorkflowEngine,
    WorkflowExecutionError,
    WorkflowStore,
    load_all_workflows,
)


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "pilk.db"
    ensure_schema(p)
    return p


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "PILK-brain"
    root.mkdir(parents=True)
    return Vault(root)


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()

    async def echo_handler(args, _ctx):
        return ToolOutcome(
            content=f"echoed: {args.get('msg', '')}",
            data={"echoed": args.get("msg", "")},
        )

    reg.register(
        Tool(
            name="echo_tool",
            description="echo for tests",
            input_schema={
                "type": "object",
                "properties": {"msg": {"type": "string"}},
            },
            risk=RiskClass.READ,
            handler=echo_handler,
        )
    )
    return reg


@pytest.fixture
def engine(
    db_path: Path,
    vault: Vault,
    registry: ToolRegistry,
) -> WorkflowEngine:
    return WorkflowEngine(
        store=WorkflowStore(db_path),
        registry=registry,
        gateway=None,
        vault=vault,
    )


# ── Manifest parsing ──────────────────────────────────────────────


def test_workflow_manifest_parses_minimal(tmp_path: Path) -> None:
    p = tmp_path / "manifest.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "name": "wf",
                "description": "x",
                "steps": [
                    {"name": "s1", "kind": "tool", "tool": "echo_tool"},
                ],
            }
        ),
        encoding="utf-8",
    )
    wf = Workflow.from_yaml(p)
    assert wf.name == "wf"
    assert wf.steps[0].kind == "tool"
    assert wf.steps[0].tool == "echo_tool"


def test_workflow_manifest_rejects_unknown_kind(tmp_path: Path) -> None:
    p = tmp_path / "manifest.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "name": "wf",
                "description": "x",
                "steps": [{"name": "bad", "kind": "magic"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        Workflow.from_yaml(p)


def test_workflow_manifest_rejects_missing_required_input(
    tmp_path: Path,
) -> None:
    p = tmp_path / "manifest.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "name": "wf",
                "description": "x",
                "inputs": [{"name": "topic", "required": True}],
                "steps": [
                    {"name": "s1", "kind": "tool", "tool": "echo_tool"},
                ],
            }
        ),
        encoding="utf-8",
    )
    wf = Workflow.from_yaml(p)
    eng = WorkflowEngine(
        store=WorkflowStore(":memory:"),
        registry=None, gateway=None, vault=None,
    )
    with pytest.raises(WorkflowExecutionError):
        # Synchronous resolution check — _resolve_inputs raises when
        # a required input is missing.
        eng._resolve_inputs(wf, {})


def test_load_all_workflows_skips_underscore_dirs(tmp_path: Path) -> None:
    (tmp_path / "_archive").mkdir()
    (tmp_path / "_archive" / "manifest.yaml").write_text(
        "name: nope\ndescription: x\nsteps:\n  - {name: s, kind: tool, "
        "tool: echo_tool}\n",
        encoding="utf-8",
    )
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "manifest.yaml").write_text(
        "name: good\ndescription: y\nsteps:\n  - {name: s, kind: tool, "
        "tool: echo_tool}\n",
        encoding="utf-8",
    )
    out = load_all_workflows(tmp_path)
    names = [w.name for w in out]
    assert "good" in names
    assert "nope" not in names


def test_default_workflows_load_ok() -> None:
    """The three default workflows shipped with this batch must
    parse cleanly. Anyone editing them will break this test if
    they introduce a YAML or schema error."""
    root = Path(__file__).resolve().parents[1] / "workflows"
    out = load_all_workflows(root)
    names = {w.name for w in out}
    assert {
        "daily_intelligence_brief",
        "ingest_file_to_brain",
        "research_topic_to_brain",
    }.issubset(names)


# ── Engine end-to-end ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_runs_tool_and_note(
    engine: WorkflowEngine, vault: Vault, tmp_path: Path,
) -> None:
    p = tmp_path / "manifest.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "name": "tool_then_note",
                "description": "tiny",
                "steps": [
                    {
                        "name": "say",
                        "kind": "tool",
                        "tool": "echo_tool",
                        "args": {"msg": "hello"},
                    },
                    {
                        "name": "save",
                        "kind": "note",
                        "path": "ingested/test/${run_id}.md",
                        "body_template": (
                            "From step: ${steps.say.text}"
                        ),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    wf = Workflow.from_yaml(p)
    run = await engine.start(wf)
    assert run.status == "completed"
    assert len(run.steps) == 2
    # The note exists with the templated content.
    note_path = run.steps[1].output["path"]
    body = (vault.root / note_path).read_text()
    assert "From step: echoed: hello" in body


@pytest.mark.asyncio
async def test_engine_pauses_on_approval(
    engine: WorkflowEngine, tmp_path: Path,
) -> None:
    p = tmp_path / "manifest.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "name": "needs_ok",
                "description": "x",
                "steps": [
                    {
                        "name": "say",
                        "kind": "tool",
                        "tool": "echo_tool",
                        "args": {"msg": "before"},
                    },
                    {
                        "name": "wait",
                        "kind": "approval",
                        "approval_message": "ok?",
                    },
                    {
                        "name": "say2",
                        "kind": "tool",
                        "tool": "echo_tool",
                        "args": {"msg": "after"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    wf = Workflow.from_yaml(p)
    run = await engine.start(wf)
    assert run.status == "paused"
    # Already executed the first tool step + recorded the approval
    # checkpoint as awaiting_approval. Resume picks up the third
    # step.
    assert any(s.kind == "approval" and s.status == "awaiting_approval"
               for s in run.steps)

    run = await engine.resume(run.id, wf)
    assert run.status == "completed"
    assert len(run.steps) == 3


@pytest.mark.asyncio
async def test_engine_records_failure_and_stops(
    engine: WorkflowEngine, tmp_path: Path,
) -> None:
    p = tmp_path / "manifest.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "name": "bad",
                "description": "x",
                "steps": [
                    {
                        "name": "miss",
                        "kind": "tool",
                        "tool": "ghost_tool",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    wf = Workflow.from_yaml(p)
    run = await engine.start(wf)
    assert run.status == "failed"
    assert "ghost_tool" in (run.error or "")


@pytest.mark.asyncio
async def test_engine_cancel_marks_run(
    engine: WorkflowEngine, tmp_path: Path,
) -> None:
    p = tmp_path / "manifest.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "name": "needs_ok",
                "description": "x",
                "steps": [
                    {
                        "name": "wait",
                        "kind": "approval",
                        "approval_message": "ok?",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    wf = Workflow.from_yaml(p)
    run = await engine.start(wf)
    assert run.status == "paused"
    await engine.cancel(run.id)
    fetched = await engine._store.get_run(run.id)
    assert fetched is not None
    assert fetched.status == "cancelled"


# ── HTTP routes ──────────────────────────────────────────────────


def test_workflow_routes_smoke(
    db_path: Path,
    vault: Vault,
    registry: ToolRegistry,
) -> None:
    from core.api.routes.workflows import router as wfr

    app = FastAPI()
    app.include_router(wfr)
    store = WorkflowStore(db_path)
    eng = WorkflowEngine(
        store=store, registry=registry, gateway=None, vault=vault,
    )
    # Build a tiny in-memory workflow registry.
    tmp = Path(__file__).parent / "_tmp_wf"
    tmp.mkdir(exist_ok=True)
    sub = tmp / "demo"
    sub.mkdir(exist_ok=True)
    (sub / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "demo",
                "description": "y",
                "steps": [
                    {
                        "name": "go",
                        "kind": "tool",
                        "tool": "echo_tool",
                        "args": {"msg": "via http"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    workflows = load_all_workflows(tmp)
    app.state.workflow_root = tmp
    app.state.workflow_store = store
    app.state.workflow_engine = eng
    app.state.workflow_registry = {w.name: w for w in workflows}
    try:
        with TestClient(app) as client:
            r = client.get("/workflows")
            assert r.status_code == 200
            assert r.json()["count"] == 1

            r = client.post(
                "/workflows/demo/run",
                json={"inputs": {}},
            )
            assert r.status_code == 200, r.text
            run_id = r.json()["id"]
            assert r.json()["status"] == "completed"

            r = client.get(f"/workflows/runs/{run_id}")
            assert r.status_code == 200

            r = client.get("/workflows/runs")
            assert r.status_code == 200
            assert r.json()["count"] >= 1
    finally:
        # Cleanup tmp manifest dir.
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
