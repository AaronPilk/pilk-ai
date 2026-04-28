"""Phase 1 cleanup — orphaned plan recovery.

When the daemon restarts (crash, SIGKILL, manual bounce), plans
that were ``running`` / ``pending`` / ``paused`` are zombies — no
live executor will progress them. ``PlanStore.recover_orphaned_plans``
flips them all to ``failed`` once at startup so the dashboard
doesn't show phantom 'running' work and Sentinel's stuck_task
signal stays clean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db.migrations import ensure_schema
from core.orchestrator import PlanStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "pilk.db"
    ensure_schema(p)
    return p


@pytest.mark.asyncio
async def test_recovery_marks_running_plans_failed(db_path: Path) -> None:
    plans = PlanStore(db_path)
    p1 = await plans.create_plan("zombie A")
    p2 = await plans.create_plan("zombie B")
    p3 = await plans.create_plan("clean — already done")
    await plans.finish_plan(p3["id"], "completed")

    # p1 + p2 stay 'running' (default for fresh plans). Recovery should
    # flip both to 'failed' and leave the completed one alone.
    recovered = await plans.recover_orphaned_plans()
    assert sorted(recovered) == sorted([p1["id"], p2["id"]])

    after_p1 = await plans.get_plan(p1["id"])
    after_p2 = await plans.get_plan(p2["id"])
    after_p3 = await plans.get_plan(p3["id"])
    assert after_p1["status"] == "failed"
    assert after_p2["status"] == "failed"
    assert after_p3["status"] == "completed"


@pytest.mark.asyncio
async def test_recovery_no_op_when_no_orphans(db_path: Path) -> None:
    plans = PlanStore(db_path)
    p = await plans.create_plan("done one")
    await plans.finish_plan(p["id"], "completed")
    recovered = await plans.recover_orphaned_plans()
    assert recovered == []


@pytest.mark.asyncio
async def test_recovery_marks_orphan_steps_failed(db_path: Path) -> None:
    """Steps that were mid-flight when the daemon died should also
    be marked failed, with a clear error explaining what happened —
    so a UI inspecting the plan history doesn't show a half-finished
    step in 'running' state forever."""
    from core.policy.risk import RiskClass

    plans = PlanStore(db_path)
    plan = await plans.create_plan("had a tool call running")
    await plans.add_step(
        plan_id=plan["id"],
        kind="tool",
        description="hypothetical tool",
        risk_class=RiskClass.READ,
        input_data={"x": 1},
    )
    # step starts as 'running' — that's the in-flight state until
    # ``finish_step`` flips it to done/failed.

    await plans.recover_orphaned_plans()

    refreshed = await plans.get_plan(plan["id"])
    assert refreshed["status"] == "failed"
    assert refreshed["steps"][0]["status"] == "failed"
    assert "orphaned" in (refreshed["steps"][0]["error"] or "")


@pytest.mark.asyncio
async def test_recovery_idempotent(db_path: Path) -> None:
    plans = PlanStore(db_path)
    await plans.create_plan("zombie")
    first = await plans.recover_orphaned_plans()
    second = await plans.recover_orphaned_plans()
    assert len(first) == 1
    assert second == []
