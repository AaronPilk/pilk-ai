"""Financial sub-policy — hard-coded, non-negotiable.

The two rules this layer enforces stay true regardless of user preference,
trust rules, or a permissive agent manifest:

1. `finance_deposit`, `finance_withdraw`, `finance_transfer`
   Always require a fresh per-call approval. Trust rules covering these
   tools are silently ignored. A human decides every time, with the exact
   amount and account visible.

2. `trade_execute`
   Callable only from a sandbox that carries the `trading` capability
   flag. Every other context is a hard reject — the user can't even be
   prompted, because the intent of the flag is "I have deliberately
   built a trading workspace; nothing else in my environment should be
   able to place orders."

New financial tools get their constraint here and here only. This file is
the list you read when you want to know what can touch money.
"""

from __future__ import annotations

from dataclasses import dataclass

NEVER_WHITELISTABLE: frozenset[str] = frozenset(
    {"finance_deposit", "finance_withdraw", "finance_transfer"}
)

TRADING_TOOLS: frozenset[str] = frozenset({"trade_execute"})
TRADING_CAPABILITY: str = "trading"


@dataclass(frozen=True)
class FinancialRuling:
    """Result of consulting the financial sub-policy for a call."""

    hard_reject: bool = False
    bypass_trust: bool = False   # if True, trust rules must be ignored
    reason: str = ""


def evaluate(
    *, tool_name: str, sandbox_capabilities: frozenset[str]
) -> FinancialRuling:
    if tool_name in TRADING_TOOLS:
        if TRADING_CAPABILITY not in sandbox_capabilities:
            return FinancialRuling(
                hard_reject=True,
                reason=(
                    f"{tool_name} requires a sandbox with the "
                    f"{TRADING_CAPABILITY!r} capability"
                ),
            )
        # trading tools inside a trading sandbox still require an approval;
        # the trust bypass guarantees the approval can't be auto-whitelisted.
        return FinancialRuling(bypass_trust=True, reason="trading tool")
    if tool_name in NEVER_WHITELISTABLE:
        return FinancialRuling(
            bypass_trust=True,
            reason="deposit/withdraw/transfer always requires fresh approval",
        )
    return FinancialRuling()
