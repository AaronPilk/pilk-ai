from core.policy import Decision, Gate, GateInput
from core.policy.risk import RiskClass


def _inp(**overrides) -> GateInput:
    base = {"tool_name": "fs_read", "risk": RiskClass.READ}
    base.update(overrides)
    return GateInput(**base)


def test_low_risk_classes_auto_allow() -> None:
    gate = Gate()
    for risk, name in (
        (RiskClass.READ, "fs_read"),
        (RiskClass.WRITE_LOCAL, "fs_write"),
        (RiskClass.EXEC_LOCAL, "shell_exec"),
        (RiskClass.NET_READ, "net_fetch"),
        (RiskClass.BROWSE, "browser_navigate"),
    ):
        outcome = gate.evaluate(_inp(tool_name=name, risk=risk))
        assert outcome.decision is Decision.ALLOW, (name, outcome.reason)


def test_outbound_actions_still_queue_for_approval() -> None:
    """The retuned policy only auto-allows sandbox-scoped work. Anything
    that leaves the sandbox — posting, messaging, finance, irreversible —
    still hits the approval queue under the default profile."""
    gate = Gate()
    for risk, name in (
        (RiskClass.NET_WRITE, "net_post"),
        (RiskClass.COMMS, "comms_send"),
        (RiskClass.IRREVERSIBLE, "rm_rf"),
    ):
        outcome = gate.evaluate(_inp(tool_name=name, risk=risk))
        assert outcome.decision is Decision.APPROVE, (name, outcome.reason)


def test_trust_rule_bypasses_approval_for_net_write() -> None:
    gate = Gate()
    gate.trust.add(
        agent_name=None,
        tool_name="net_post",
        args_matcher={"url": "https://example.com"},
        ttl_seconds=60,
    )
    # Matching args — ALLOW via trust rule.
    outcome = gate.evaluate(
        _inp(
            tool_name="net_post",
            risk=RiskClass.NET_WRITE,
            args={"url": "https://example.com"},
        )
    )
    assert outcome.decision is Decision.ALLOW
    # Different URL — still APPROVE.
    outcome = gate.evaluate(
        _inp(
            tool_name="net_post",
            risk=RiskClass.NET_WRITE,
            args={"url": "https://other.example"},
        )
    )
    assert outcome.decision is Decision.APPROVE


def test_operator_profile_widens_net_write() -> None:
    def profile_for(agent: str | None) -> str:
        return "operator" if agent == "sales_bot" else "assistant"

    gate = Gate(agent_profile_lookup=profile_for)
    # Default profile (assistant) — NET_WRITE still approval-gated.
    assert (
        gate.evaluate(
            _inp(tool_name="net_post", risk=RiskClass.NET_WRITE)
        ).decision
        is Decision.APPROVE
    )
    # Operator profile — NET_WRITE auto-allowed.
    assert (
        gate.evaluate(
            _inp(
                tool_name="net_post",
                risk=RiskClass.NET_WRITE,
                agent_name="sales_bot",
            )
        ).decision
        is Decision.ALLOW
    )
    # But FINANCIAL and IRREVERSIBLE still require approval even for operator.
    assert (
        gate.evaluate(
            _inp(
                tool_name="rm_rf",
                risk=RiskClass.IRREVERSIBLE,
                agent_name="sales_bot",
            )
        ).decision
        is Decision.APPROVE
    )


def test_autonomous_profile_also_widens_comms() -> None:
    def profile_for(agent: str | None) -> str:
        return "autonomous" if agent == "sdr" else "assistant"

    gate = Gate(agent_profile_lookup=profile_for)
    # COMMS auto-allowed under autonomous (for outbound sales flow).
    assert (
        gate.evaluate(
            _inp(
                tool_name="gmail_send",
                risk=RiskClass.COMMS,
                agent_name="sdr",
            )
        ).decision
        is Decision.ALLOW
    )
    # Under default profile, COMMS is still gated.
    assert (
        gate.evaluate(
            _inp(tool_name="gmail_send", risk=RiskClass.COMMS)
        ).decision
        is Decision.APPROVE
    )
