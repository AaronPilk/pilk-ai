"""Policy gate — the single decision point in front of tool execution.

Given (tool, args, context), the gate returns one of three decisions:

  ALLOW   — run the tool immediately
  APPROVE — ask the user; run the tool only if they approve
  REJECT  — refuse outright, no prompt

Decision order (first match wins):

  1. Financial sub-policy (hard constraints; cannot be whitelisted)
  2. Trust rule match → ALLOW (unless sub-policy set bypass_trust)
  3. Risk-class auto-allow: READ, WRITE_LOCAL, EXEC_LOCAL in scope
  4. Otherwise APPROVE — the approval queue makes the call

`GateInput` is a small value object so the gate stays testable without
needing a real ToolContext.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from core.policy import financial
from core.policy.risk import RiskClass
from core.policy.trust import TrustStore


class Decision(StrEnum):
    ALLOW = "allow"
    APPROVE = "approve"
    REJECT = "reject"


@dataclass
class PolicyOutcome:
    decision: Decision
    reason: str = ""
    bypass_trust: bool = False


@dataclass(frozen=True)
class GateInput:
    tool_name: str
    risk: RiskClass
    args: dict[str, Any] = field(default_factory=dict)
    agent_name: str | None = None
    sandbox_capabilities: frozenset[str] = field(default_factory=frozenset)


AUTO_ALLOW: frozenset[RiskClass] = frozenset(
    {RiskClass.READ, RiskClass.WRITE_LOCAL, RiskClass.EXEC_LOCAL}
)


class Gate:
    """Policy front door. Stateless aside from the injected trust store."""

    def __init__(self, trust: TrustStore | None = None) -> None:
        self.trust = trust or TrustStore()

    def evaluate(self, inp: GateInput) -> PolicyOutcome:
        # 1) Financial sub-policy first — hard rejects and trust bypass.
        ruling = financial.evaluate(
            tool_name=inp.tool_name,
            sandbox_capabilities=inp.sandbox_capabilities,
        )
        if ruling.hard_reject:
            return PolicyOutcome(
                decision=Decision.REJECT, reason=ruling.reason
            )

        # 2) Trust rules, unless the sub-policy forbids them for this call.
        if not ruling.bypass_trust:
            rule = self.trust.match(
                agent_name=inp.agent_name,
                tool_name=inp.tool_name,
                args=inp.args,
            )
            if rule is not None:
                return PolicyOutcome(
                    decision=Decision.ALLOW,
                    reason=f"trust rule {rule.id} (uses={rule.uses})",
                )

        # 3) Risk-based auto-allow for low-risk, locally-scoped work.
        if inp.risk in AUTO_ALLOW:
            return PolicyOutcome(
                decision=Decision.ALLOW,
                reason=f"{inp.risk.value}: auto-allow (workspace scope)",
            )

        # 4) Everything else queues for user approval.
        return PolicyOutcome(
            decision=Decision.APPROVE,
            reason=(ruling.reason or f"{inp.risk.value}: requires approval"),
            bypass_trust=ruling.bypass_trust,
        )
