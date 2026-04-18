"""Policy gate — the single decision point in front of tool execution.

Given (tool, args, context), the gate returns one of three decisions:

  ALLOW   — run the tool immediately
  APPROVE — ask the user; run the tool only if they approve
  REJECT  — refuse outright, no prompt

Decision order (first match wins):

  1. Financial sub-policy (hard constraints; cannot be whitelisted)
  2. Trust rule match → ALLOW (unless sub-policy set bypass_trust)
  3. Agent autonomy profile widens the auto-allow set
  4. Risk-class auto-allow:
     default — READ, WRITE_LOCAL, EXEC_LOCAL, NET_READ, BROWSE
  5. Otherwise APPROVE — the approval queue makes the call

Design principle: approve outcomes, not mechanics. Once the user has
given a task, PILK may freely read, write-local, and browse inside its
sandbox. Approvals are reserved for actions that leave the sandbox and
touch a real person, real money, or permanent state.

`GateInput` is a small value object so the gate stays testable without
needing a real ToolContext.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from core.policy import comms, financial, system
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
    {
        RiskClass.READ,
        RiskClass.WRITE_LOCAL,
        RiskClass.EXEC_LOCAL,
        RiskClass.NET_READ,
        RiskClass.BROWSE,
    }
)

# Per-agent-autonomy-profile widening. Each profile *adds* risk classes
# to the base AUTO_ALLOW above. FINANCIAL and IRREVERSIBLE never appear
# — those always require a fresh approval regardless of profile.
PROFILE_AUTO_ALLOW: dict[str, frozenset[RiskClass]] = {
    "observer": frozenset(),
    "assistant": frozenset(),
    "operator": frozenset({RiskClass.NET_WRITE}),
    "autonomous": frozenset({RiskClass.NET_WRITE, RiskClass.COMMS}),
}
DEFAULT_PROFILE = "assistant"


class Gate:
    """Policy front door. Stateless aside from the injected trust store.

    Optionally takes an `agent_profile_lookup` callable that returns the
    autonomy profile for a given agent name (or None for top-level
    chat). If not provided, every agent uses the default profile.
    """

    def __init__(
        self,
        trust: TrustStore | None = None,
        *,
        agent_profile_lookup: Any = None,
    ) -> None:
        self.trust = trust or TrustStore()
        self._agent_profile_lookup = agent_profile_lookup

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

        # 2) System sub-policy — tools that modify PILK itself.
        sys_ruling = system.evaluate(tool_name=inp.tool_name)
        # 2b) Comms sub-policy — user-identity outbound messages must
        # always hit the approval queue freshly, never via trust rules.
        comms_ruling = comms.evaluate(tool_name=inp.tool_name)
        bypass_trust = (
            ruling.bypass_trust
            or sys_ruling.bypass_trust
            or comms_ruling.bypass_trust
        )
        if sys_ruling.requires_approval:
            return PolicyOutcome(
                decision=Decision.APPROVE,
                reason=sys_ruling.reason,
                bypass_trust=True,
            )

        # 3) Trust rules, unless a sub-policy forbids them.
        if not bypass_trust:
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

        # 4) Risk-based auto-allow for low-risk, locally-scoped work,
        #    plus anything the agent's autonomy profile widens. If a
        #    sub-policy (comms-as-the-user, system-level changes) set
        #    bypass_trust, those tools always need a fresh decision —
        #    profile widening cannot override the NEVER_WHITELISTABLE
        #    invariants.
        if not bypass_trust:
            profile = self._profile_for(inp.agent_name)
            allowed = AUTO_ALLOW | PROFILE_AUTO_ALLOW.get(profile, frozenset())
            if inp.risk in allowed:
                extra = " via profile" if inp.risk not in AUTO_ALLOW else ""
                return PolicyOutcome(
                    decision=Decision.ALLOW,
                    reason=f"{inp.risk.value}: auto-allow{extra} ({profile})",
                )

        # 5) Everything else queues for user approval.
        return PolicyOutcome(
            decision=Decision.APPROVE,
            reason=(
                comms_ruling.reason
                or ruling.reason
                or f"{inp.risk.value}: requires approval"
            ),
            bypass_trust=bypass_trust,
        )

    def _profile_for(self, agent_name: str | None) -> str:
        if self._agent_profile_lookup is None:
            return DEFAULT_PROFILE
        try:
            profile = self._agent_profile_lookup(agent_name)
        except Exception:
            return DEFAULT_PROFILE
        if profile not in PROFILE_AUTO_ALLOW:
            return DEFAULT_PROFILE
        return profile
