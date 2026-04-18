"""Cancellation path: orchestrator.cancel_plan + approvals.cancel_plan.

Covers:
  - A plan blocked on an approval unblocks as soon as the user cancels.
  - The plan ends with status='cancelled' (not 'failed').
  - Approvals for other plans are not collateral-damaged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from core.config import get_settings
from core.db import ensure_schema
from core.governor.providers import AnthropicPlannerProvider
from core.ledger import Ledger
from core.orchestrator import Orchestrator, PlanStore
from core.policy import ApprovalManager, Gate, TrustStore
from core.policy.risk import RiskClass
from core.tools import Gateway, ToolRegistry
from core.tools.registry import Tool, ToolContext, ToolOutcome


@dataclass
class _Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict | None = None


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _Response:
    content: list[_Block]
    stop_reason: str
    usage: _Usage


class _StubMessages:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)

    async def create(self, **_kwargs):
        return self._responses.pop(0)


class _StubClient:
    def __init__(self, responses: list[_Response]) -> None:
        self.messages = _StubMessages(responses)

    async def close(self):  # pragma: no cover — only for parity
        pass


async def _handler(_args: dict, _ctx: ToolContext) -> ToolOutcome:
    return ToolOutcome(content="done", data={})


# A tool classified as NET_WRITE so it always queues for approval.
_OUTBOUND_TOOL = Tool(
    name="net_post",
    description="fake outbound write tool used only in cancel tests",
    input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
    risk=RiskClass.NET_WRITE,
    handler=_handler,
)


@pytest.mark.asyncio
async def test_cancel_unblocks_pending_approval_and_marks_plan_cancelled() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)

    # The planner wants to call net_post once; it never gets to turn 2 because
    # we cancel while it's waiting on the approval.
    responses = [
        _Response(
            content=[
                _Block(
                    type="tool_use",
                    id="tu_1",
                    name="net_post",
                    input={"url": "https://example.com"},
                )
            ],
            stop_reason="tool_use",
            usage=_Usage(input_tokens=10, output_tokens=5),
        ),
        # Safety response in case the loop ever reaches turn 2; the cancel
        # event should fire first, so this shouldn't be consumed.
        _Response(
            content=[_Block(type="text", text="done")],
            stop_reason="end_turn",
            usage=_Usage(input_tokens=5, output_tokens=5),
        ),
    ]

    ledger = Ledger(settings.db_path)
    plans = PlanStore(settings.db_path)
    registry = ToolRegistry()
    registry.register(_OUTBOUND_TOOL)
    trust = TrustStore()
    approvals = ApprovalManager(db_path=settings.db_path, trust_store=trust)
    gateway = Gateway(registry, Gate(trust=trust), approvals=approvals)

    events: list[tuple[str, dict]] = []

    async def broadcast(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    client = _StubClient(responses)
    orch = Orchestrator(
        client=client,
        registry=registry,
        gateway=gateway,
        ledger=ledger,
        plans=plans,
        broadcast=broadcast,
        planner_model="claude-opus-4-7",
        max_turns=4,
        providers={"anthropic": AnthropicPlannerProvider(client)},
    )

    run = asyncio.create_task(orch.run("post something"))

    # Wait until the gateway has queued the approval request.
    for _ in range(200):
        if orch.running_plan_id is not None and await approvals.pending_list():
            break
        await asyncio.sleep(0.01)
    else:
        run.cancel()
        raise AssertionError("approval never queued")

    plan_id = orch.running_plan_id
    assert plan_id is not None

    cancelled = await orch.cancel_plan(plan_id, reason="user pressed stop")
    assert cancelled is True

    # The orchestrator should unblock and finish within a tight window.
    await asyncio.wait_for(run, timeout=2.0)

    # Plan ended with status=cancelled (not failed).
    stored = await plans.get_plan(plan_id)
    assert stored["status"] == "cancelled"

    # plan.completed event carries the cancelled status.
    [(_, completion)] = [e for e in events if e[0] == "plan.completed"]
    assert completion["status"] == "cancelled"
    assert completion.get("cancelled_reason") == "user pressed stop"

    # No lingering pending approval.
    assert (await approvals.pending_list()) == []


@pytest.mark.asyncio
async def test_cancel_plan_returns_false_when_not_running() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)

    ledger = Ledger(settings.db_path)
    plans = PlanStore(settings.db_path)
    registry = ToolRegistry()
    trust = TrustStore()
    approvals = ApprovalManager(db_path=settings.db_path, trust_store=trust)
    gateway = Gateway(registry, Gate(trust=trust), approvals=approvals)

    async def broadcast(_et: str, _p: dict) -> None:
        return None

    client = _StubClient([])
    orch = Orchestrator(
        client=client,
        registry=registry,
        gateway=gateway,
        ledger=ledger,
        plans=plans,
        broadcast=broadcast,
        planner_model="claude-opus-4-7",
        max_turns=2,
        providers={"anthropic": AnthropicPlannerProvider(client)},
    )
    assert await orch.cancel_plan("plan_does_not_exist") is False


@pytest.mark.asyncio
async def test_cancel_plan_only_touches_own_approvals() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)

    plans = PlanStore(settings.db_path)
    mine = await plans.create_plan("mine")
    other = await plans.create_plan("other")

    trust = TrustStore()
    approvals = ApprovalManager(db_path=settings.db_path, trust_store=trust)

    req_mine = await approvals.request(
        plan_id=mine["id"],
        step_id=None,
        agent_name=None,
        tool_name="net_post",
        args={},
        risk_class=RiskClass.NET_WRITE,
        reason="",
    )
    req_other = await approvals.request(
        plan_id=other["id"],
        step_id=None,
        agent_name=None,
        tool_name="net_post",
        args={},
        risk_class=RiskClass.NET_WRITE,
        reason="",
    )

    cancelled = await approvals.cancel_plan(mine["id"], reason="stop")
    assert req_mine.id in cancelled
    assert req_other.id not in cancelled

    # The other plan's approval is still pending.
    pending_ids = [r["id"] for r in await approvals.pending_list()]
    assert req_other.id in pending_ids
    assert req_mine.id not in pending_ids

    # Drain the other one so the test exits clean.
    await approvals.reject(req_other.id, reason="cleanup")
