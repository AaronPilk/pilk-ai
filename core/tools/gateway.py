"""Tool gateway.

Every tool call — whether initiated by the orchestrator or a future agent —
passes through here. The gateway evaluates policy, invokes the handler,
and returns a normalized `ToolResult`. This is the single choke point for
risk enforcement and cost attribution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.logging import get_logger
from core.policy import Decision, Gate
from core.tools.registry import ToolContext, ToolRegistry

log = get_logger("pilkd.gateway")


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
    def __init__(self, registry: ToolRegistry, gate: Gate) -> None:
        self.registry = registry
        self.gate = gate

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

        outcome = self.gate.evaluate(tool.risk)
        if outcome.decision is not Decision.ALLOW:
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
