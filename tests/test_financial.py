"""Hard-coded financial sub-policy tests.

The guarantees this file locks in:
  - trade_execute outside a `trading` sandbox: hard REJECT (no prompt).
  - trade_execute inside a `trading` sandbox: APPROVE, and the request
    is marked bypass_trust so no trust rule can ever be installed.
  - deposit/withdraw/transfer: always APPROVE, always bypass_trust —
    a user cannot whitelist them, period.
"""

from core.policy import Decision, Gate, GateInput
from core.policy.risk import RiskClass


def _trade(caps: frozenset[str]) -> GateInput:
    return GateInput(
        tool_name="trade_execute",
        risk=RiskClass.FINANCIAL,
        args={"side": "buy", "symbol": "VOO", "quantity": 1},
        sandbox_capabilities=caps,
    )


def test_trade_execute_hard_rejected_without_capability() -> None:
    gate = Gate()
    outcome = gate.evaluate(_trade(frozenset()))
    assert outcome.decision is Decision.REJECT
    assert "trading" in outcome.reason


def test_trade_execute_approves_with_capability() -> None:
    gate = Gate()
    outcome = gate.evaluate(_trade(frozenset({"trading"})))
    assert outcome.decision is Decision.APPROVE
    assert outcome.bypass_trust is True


def test_deposit_withdraw_transfer_bypass_trust() -> None:
    gate = Gate()
    for tool in ("finance_deposit", "finance_withdraw", "finance_transfer"):
        outcome = gate.evaluate(
            GateInput(
                tool_name=tool,
                risk=RiskClass.FINANCIAL,
                args={"amount_usd": 100, "account": "a"},
            )
        )
        assert outcome.decision is Decision.APPROVE, tool
        assert outcome.bypass_trust is True, tool


def test_trust_rule_cannot_override_financial() -> None:
    gate = Gate()
    # Even with a trust rule installed for finance_deposit, the sub-policy
    # forces bypass_trust and the gate returns APPROVE.
    gate.trust.add(
        agent_name=None,
        tool_name="finance_deposit",
        args_matcher={"amount_usd": 5},
        ttl_seconds=60,
    )
    outcome = gate.evaluate(
        GateInput(
            tool_name="finance_deposit",
            risk=RiskClass.FINANCIAL,
            args={"amount_usd": 5, "account": "a"},
        )
    )
    assert outcome.decision is Decision.APPROVE
    assert outcome.bypass_trust is True
