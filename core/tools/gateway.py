"""Tool gateway.

Every tool call passes through here. The gateway:

  1. Looks the tool up in the registry.
  2. Asks the policy gate what to do (ALLOW / APPROVE / REJECT).
  3. On APPROVE: opens an approval request and awaits the user's decision.
     During that wait the current step is marked `awaiting_approval` so
     the dashboard can show the pause.
  4. Runs the handler, normalises the result, returns it.

The gateway is the only place that pauses the orchestrator. Keeping the
wait inside gateway.execute means the orchestrator loop stays a simple
sequential pipeline — it just sees a tool call that took a while.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from core.logging import get_logger
from core.policy import ApprovalManager, Decision, Gate, GateInput
from core.tools.registry import ToolContext, ToolRegistry

log = get_logger("pilkd.gateway")

StepStatusCallback = Callable[[str, str], Awaitable[None]]
"""(step_id, status) — called when the gateway toggles awaiting_approval."""


@dataclass
class ToolResult:
    tool: str
    ok: bool
    content: str
    is_error: bool
    data: dict[str, Any]
    risk: str
    rejection_reason: str | None = None


class Gateway:
    def __init__(
        self,
        registry: ToolRegistry,
        gate: Gate,
        *,
        approvals: ApprovalManager | None = None,
        on_step_status: StepStatusCallback | None = None,
    ) -> None:
        self.registry = registry
        self.gate = gate
        self.approvals = approvals
        self.on_step_status = on_step_status

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        ctx = ctx or ToolContext()
        tool = self.registry.get(name)
        if tool is None:
            msg = f"unknown tool: {name}"
            log.warning("tool_unknown", tool=name)
            return ToolResult(
                tool=name, ok=False, content=msg, is_error=True, data={}, risk="",
            )

        outcome = self.gate.evaluate(
            GateInput(
                tool_name=name,
                risk=tool.risk,
                args=args,
                agent_name=ctx.agent_name,
                sandbox_capabilities=ctx.sandbox_capabilities,
            )
        )

        if outcome.decision is Decision.REJECT:
            log.info(
                "tool_rejected",
                tool=name,
                risk=tool.risk.value,
                reason=outcome.reason,
            )
            return ToolResult(
                tool=name,
                ok=False,
                content=f"refused: {outcome.reason}",
                is_error=True,
                data={},
                risk=tool.risk.value,
                rejection_reason=outcome.reason,
            )

        if outcome.decision is Decision.APPROVE:
            if self.approvals is None:
                reason = "approval required but approval manager is offline"
                log.warning("approval_unavailable", tool=name)
                return ToolResult(
                    tool=name,
                    ok=False,
                    content=f"refused: {reason}",
                    is_error=True,
                    data={},
                    risk=tool.risk.value,
                    rejection_reason=reason,
                )
            if ctx.step_id and self.on_step_status:
                await self.on_step_status(ctx.step_id, "awaiting_approval")
            request = await self.approvals.request(
                plan_id=ctx.plan_id,
                step_id=ctx.step_id,
                agent_name=ctx.agent_name,
                tool_name=name,
                args=args,
                risk_class=tool.risk,
                reason=outcome.reason,
                bypass_trust=outcome.bypass_trust,
            )
            decision = await request.future
            if ctx.step_id and self.on_step_status:
                await self.on_step_status(ctx.step_id, "running")
            if decision.decision != "approved":
                reason = f"user rejected: {decision.reason or 'no reason given'}"
                return ToolResult(
                    tool=name,
                    ok=False,
                    content=f"refused: {reason}",
                    is_error=True,
                    data={},
                    risk=tool.risk.value,
                    rejection_reason=reason,
                )

        log.info("tool_invoke", tool=name, risk=tool.risk.value)
        try:
            result = await tool.handler(args, ctx)
        except Exception as e:
            log.exception("tool_crashed", tool=name)
            return ToolResult(
                tool=name,
                ok=False,
                content=f"tool crashed: {type(e).__name__}: {e}",
                is_error=True,
                data={},
                risk=tool.risk.value,
            )

        return ToolResult(
            tool=name,
            ok=not result.is_error,
            content=result.content,
            is_error=result.is_error,
            data=result.data,
            risk=tool.risk.value,
        )
