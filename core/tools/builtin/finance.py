"""Financial stub tools.

These are placeholders for a real bank/broker integration — they do not
move actual money. They exist so the financial sub-policy has concrete
call surfaces to enforce:

  - finance_deposit / finance_withdraw / finance_transfer
    Hard-coded FINANCIAL risk. Never eligible for a trust whitelist; every
    call requires a fresh approval.
  - trade_execute
    Only allowed when the caller's sandbox carries the `trading` capability
    flag. Refused otherwise, even if a user would approve it.
"""

from __future__ import annotations

from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome


async def _deposit(args: dict, ctx: ToolContext) -> ToolOutcome:
    amount = float(args["amount_usd"])
    account = str(args["account"])
    return ToolOutcome(
        content=f"[stub] deposit ${amount:.2f} → {account}",
        data={"amount_usd": amount, "account": account, "stub": True},
    )


async def _withdraw(args: dict, ctx: ToolContext) -> ToolOutcome:
    amount = float(args["amount_usd"])
    account = str(args["account"])
    return ToolOutcome(
        content=f"[stub] withdraw ${amount:.2f} ← {account}",
        data={"amount_usd": amount, "account": account, "stub": True},
    )


async def _transfer(args: dict, ctx: ToolContext) -> ToolOutcome:
    amount = float(args["amount_usd"])
    src = str(args["from_account"])
    dst = str(args["to_account"])
    return ToolOutcome(
        content=f"[stub] transfer ${amount:.2f}: {src} → {dst}",
        data={
            "amount_usd": amount,
            "from_account": src,
            "to_account": dst,
            "stub": True,
        },
    )


async def _trade_execute(args: dict, ctx: ToolContext) -> ToolOutcome:
    side = str(args["side"])
    symbol = str(args["symbol"])
    qty = float(args["quantity"])
    return ToolOutcome(
        content=f"[stub] {side.upper()} {qty} {symbol}",
        data={"side": side, "symbol": symbol, "quantity": qty, "stub": True},
    )


_AMOUNT = {"type": "number", "minimum": 0.01}
_ACCOUNT = {"type": "string", "description": "Opaque account identifier."}


finance_deposit_tool = Tool(
    name="finance_deposit",
    description=(
        "Deposit funds into an account. FINANCIAL risk — every invocation "
        "requires fresh user approval (never eligible for a trust rule)."
    ),
    input_schema={
        "type": "object",
        "properties": {"amount_usd": _AMOUNT, "account": _ACCOUNT},
        "required": ["amount_usd", "account"],
    },
    risk=RiskClass.FINANCIAL,
    handler=_deposit,
)


finance_withdraw_tool = Tool(
    name="finance_withdraw",
    description=(
        "Withdraw funds from an account. FINANCIAL risk — every invocation "
        "requires fresh user approval (never eligible for a trust rule)."
    ),
    input_schema={
        "type": "object",
        "properties": {"amount_usd": _AMOUNT, "account": _ACCOUNT},
        "required": ["amount_usd", "account"],
    },
    risk=RiskClass.FINANCIAL,
    handler=_withdraw,
)


finance_transfer_tool = Tool(
    name="finance_transfer",
    description=(
        "Transfer funds between accounts. FINANCIAL risk — every invocation "
        "requires fresh user approval (never eligible for a trust rule)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "amount_usd": _AMOUNT,
            "from_account": _ACCOUNT,
            "to_account": _ACCOUNT,
        },
        "required": ["amount_usd", "from_account", "to_account"],
    },
    risk=RiskClass.FINANCIAL,
    handler=_transfer,
)


trade_execute_tool = Tool(
    name="trade_execute",
    description=(
        "Place a market trade. Only callable from a sandbox flagged with the "
        "`trading` capability; refused elsewhere regardless of user approval."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "side": {"type": "string", "enum": ["buy", "sell"]},
            "symbol": {"type": "string"},
            "quantity": {"type": "number", "minimum": 0},
        },
        "required": ["side", "symbol", "quantity"],
    },
    risk=RiskClass.FINANCIAL,
    handler=_trade_execute,
)
