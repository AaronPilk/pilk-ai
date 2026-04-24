"""Trade-execution stub — the ONLY financial-surface tool PILK ships.

Hard rules the operator set (2026-04-24):

* PILK has zero ability to deposit, withdraw, or transfer money.
  finance_deposit / finance_withdraw / finance_transfer are NOT
  registered — they've been deleted from the tool surface entirely.
  If a future code path needs money movement, add a new tool and
  route it through a separate approval gate + operator consent flow
  rather than re-enabling the old stubs.
* trade_execute is gated by the sandbox ``trading`` capability.
  Only the xauusd_execution_agent carries that capability, and even
  there live execution requires ``core.trading.xauusd.config
  .LIVE_TRADING_ENABLED = True`` which is hard-coded False. So
  trade_execute in practice is paper-mode only.

This module used to host finance_deposit/withdraw/transfer stubs;
those are removed entirely. The trade_execute stub stays so the
XAUUSD paper-trading loop has a call surface to close against.
"""

from __future__ import annotations

from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome


async def _trade(args: dict, ctx: ToolContext) -> ToolOutcome:
    # Policy layer refuses this call unless the sandbox carries the
    # 'trading' capability (see core/policy/system.py). So reaching
    # this coroutine at all means the caller was already authorised.
    symbol = str(args["symbol"])
    side = str(args["side"])
    size = float(args["size"])
    return ToolOutcome(
        content=f"[stub] {side.upper()} {size} {symbol}",
        data={"symbol": symbol, "side": side, "size": size, "stub": True},
    )


trade_execute_tool = Tool(
    name="trade_execute",
    description=(
        "Execute a paper trade. Only callable from a sandbox carrying "
        "the `trading` capability (xauusd_execution_agent). Live "
        "execution is hard-gated at the trading module level; this "
        "tool stays a stub until that flag flips."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "side": {"type": "string", "enum": ["buy", "sell"]},
            "size": {"type": "number"},
        },
        "required": ["symbol", "side", "size"],
    },
    risk=RiskClass.FINANCIAL,
    handler=_trade,
)
