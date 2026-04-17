"""Comms sub-policy — sending as the user must never be auto-approved."""

from __future__ import annotations

from core.policy.gate import Decision, Gate, GateInput
from core.policy.risk import RiskClass
from core.policy.trust import TrustStore


def test_gmail_send_as_me_forces_approval_and_bypasses_trust() -> None:
    # Install a broad trust rule that would normally cover gmail_send_as_me.
    trust = TrustStore()
    trust.add(
        agent_name=None,
        tool_name="gmail_send_as_me",
        ttl_seconds=3600,
    )
    gate = Gate(trust=trust)

    outcome = gate.evaluate(
        GateInput(
            tool_name="gmail_send_as_me",
            risk=RiskClass.COMMS,
            args={"to": "client@example.com", "subject": "hi", "body": "hi"},
        ),
    )
    assert outcome.decision == Decision.APPROVE
    assert outcome.bypass_trust is True


def test_gmail_send_as_pilk_can_be_trust_covered() -> None:
    trust = TrustStore()
    trust.add(
        agent_name=None,
        tool_name="gmail_send_as_pilk",
        ttl_seconds=3600,
    )
    gate = Gate(trust=trust)

    outcome = gate.evaluate(
        GateInput(
            tool_name="gmail_send_as_pilk",
            risk=RiskClass.COMMS,
            args={"to": "aaron@example.com", "subject": "report", "body": "..."},
        ),
    )
    assert outcome.decision == Decision.ALLOW
    assert outcome.bypass_trust is False
