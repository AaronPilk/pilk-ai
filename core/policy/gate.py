"""Minimal policy gate for batch 1.

Only READ, WRITE_LOCAL, EXEC_LOCAL are auto-allowed and only for calls
scoped to the PILK workspace. Anything else is rejected immediately — the
approval queue and user-configurable policies arrive in batch 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from core.policy.risk import RiskClass


class Decision(StrEnum):
    ALLOW = "allow"
    REJECT = "reject"
    APPROVE = "approve"  # reserved for batch 3


@dataclass
class PolicyOutcome:
    decision: Decision
    reason: str = ""


class Policy:
    """A default policy that auto-allows low-risk local actions."""

    AUTO_ALLOW: ClassVar[set[RiskClass]] = {
        RiskClass.READ,
        RiskClass.WRITE_LOCAL,
        RiskClass.EXEC_LOCAL,
    }

    def evaluate(self, risk: RiskClass) -> PolicyOutcome:
        if risk in self.AUTO_ALLOW:
            return PolicyOutcome(Decision.ALLOW, "auto-allow (workspace scope)")
        return PolicyOutcome(
            Decision.REJECT,
            f"{risk.value}: approval UI not implemented until batch 3",
        )


class Gate:
    """Thin wrapper exposing a single `evaluate(risk)` entry point.

    Indirection is intentional — batch 3 will replace this with a richer
    gate that consults agent/sandbox policies and the approval queue.
    """

    def __init__(self, policy: Policy | None = None) -> None:
        self.policy = policy or Policy()

    def evaluate(self, risk: RiskClass) -> PolicyOutcome:
        return self.policy.evaluate(risk)
