"""Approval manager + gateway integration tests.

Covers the full pause-and-resume loop without an Anthropic client:
  - Gateway pauses on APPROVE, awaits the user, then runs the tool.
  - Approve with a trust rule → the rule unlocks the next call of the
    same shape.
  - Reject returns the "refused" ToolResult.
  - Batch approve drains non-financial pending items.
  - Financial items cannot install trust rules through an approval.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.config import get_settings
from core.db import ensure_schema
from core.policy import ApprovalManager, Gate, TrustStore
from core.policy.risk import RiskClass
from core.tools import Gateway, ToolRegistry
from core.tools.builtin import finance_deposit_tool, net_fetch_tool
from core.tools.registry import Tool, ToolContext, ToolOutcome


async def _fake_fetch(args: dict, ctx: ToolContext) -> ToolOutcome:
    return ToolOutcome(content=f"ok {args.get('url')}", data={"status": 200})


FAKE_NET_FETCH = Tool(
    name="net_fetch",
    description=net_fetch_tool.description,
    input_schema=net_fetch_tool.input_schema,
    risk=RiskClass.NET_READ,
    handler=_fake_fetch,
)


async def _fake_deposit(args: dict, ctx: ToolContext) -> ToolOutcome:
    return ToolOutcome(content="ok deposit", data={"amount_usd": args["amount_usd"]})


FAKE_DEPOSIT = Tool(
    name="finance_deposit",
    description=finance_deposit_tool.description,
    input_schema=finance_deposit_tool.input_schema,
    risk=RiskClass.FINANCIAL,
    handler=_fake_deposit,
)


def _wire(db_path: Path) -> tuple[Gateway, ApprovalManager, TrustStore]:
    registry = ToolRegistry()
    registry.register(FAKE_NET_FETCH)
    registry.register(FAKE_DEPOSIT)
    trust = TrustStore()
    approvals = ApprovalManager(db_path=db_path, trust_store=trust)
    gate = Gate(trust=trust)
    return Gateway(registry, gate, approvals=approvals), approvals, trust


@pytest.mark.asyncio
async def test_approval_allow_flow_runs_tool() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    gw, mgr, _ = _wire(settings.db_path)

    async def approve_when_ready() -> None:
        # Wait until the gateway has actually queued the request.
        for _ in range(50):
            pending = await mgr.pending_list()
            if pending:
                await mgr.approve(pending[0]["id"], reason="ok")
                return
            await asyncio.sleep(0.01)
        raise AssertionError("gateway never queued an approval")

    task = asyncio.create_task(
        gw.execute("net_fetch", {"url": "https://x.example"})
    )
    await approve_when_ready()
    result = await task
    assert result.ok is True
    assert "https://x.example" in result.content


@pytest.mark.asyncio
async def test_approval_reject_returns_refused() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    gw, mgr, _ = _wire(settings.db_path)

    async def reject_when_ready() -> None:
        for _ in range(50):
            pending = await mgr.pending_list()
            if pending:
                await mgr.reject(pending[0]["id"], reason="nope")
                return
            await asyncio.sleep(0.01)
        raise AssertionError("no request queued")

    task = asyncio.create_task(
        gw.execute("net_fetch", {"url": "https://x.example"})
    )
    await reject_when_ready()
    result = await task
    assert result.ok is False
    assert "refused" in result.content
    assert "nope" in (result.rejection_reason or "")


@pytest.mark.asyncio
async def test_approval_with_trust_unlocks_future_calls() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    gw, mgr, trust = _wire(settings.db_path)

    async def approve_with_trust() -> None:
        for _ in range(50):
            pending = await mgr.pending_list()
            if pending:
                await mgr.approve(
                    pending[0]["id"],
                    reason="trust it",
                    trust={"scope": "agent+args", "ttl_seconds": 60},
                )
                return
            await asyncio.sleep(0.01)
        raise AssertionError("no request queued")

    task = asyncio.create_task(
        gw.execute("net_fetch", {"url": "https://x.example"})
    )
    await approve_with_trust()
    first = await task
    assert first.ok is True
    assert len(trust.list()) == 1

    # Second call with matching args must not queue; it runs straight through.
    second = await asyncio.wait_for(
        gw.execute("net_fetch", {"url": "https://x.example"}), timeout=1.0
    )
    assert second.ok is True
    assert (await mgr.pending_list()) == []


@pytest.mark.asyncio
async def test_financial_call_cannot_install_trust_rule() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    gw, mgr, trust = _wire(settings.db_path)

    async def approve_with_trust_attempt() -> None:
        for _ in range(50):
            pending = await mgr.pending_list()
            if pending:
                await mgr.approve(
                    pending[0]["id"],
                    reason="one time",
                    trust={"scope": "agent+args", "ttl_seconds": 3600},
                )
                return
            await asyncio.sleep(0.01)
        raise AssertionError("no request queued")

    task = asyncio.create_task(
        gw.execute("finance_deposit", {"amount_usd": 100, "account": "a"})
    )
    await approve_with_trust_attempt()
    await task
    # No rule was installed — the financial sub-policy forbids it.
    assert trust.list() == []


@pytest.mark.asyncio
async def test_batch_approve_skips_financial() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    gw, mgr, _ = _wire(settings.db_path)

    t1 = asyncio.create_task(gw.execute("net_fetch", {"url": "https://a.example"}))
    t2 = asyncio.create_task(
        gw.execute("finance_deposit", {"amount_usd": 1, "account": "a"})
    )

    # Wait for both to land in the queue.
    for _ in range(50):
        if len(await mgr.pending_list()) == 2:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("requests never queued")

    approved = await mgr.approve_batch(reason="batch")
    assert len(approved) == 1
    net_result = await asyncio.wait_for(t1, timeout=1.0)
    assert net_result.ok is True
    # Financial request is still pending.
    remaining = await mgr.pending_list()
    assert len(remaining) == 1
    assert remaining[0]["tool_name"] == "finance_deposit"
    # Drain it so the test exits cleanly.
    await mgr.reject(remaining[0]["id"], reason="cleanup")
    t2_result = await asyncio.wait_for(t2, timeout=1.0)
    assert t2_result.ok is False
