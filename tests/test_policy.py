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
    ):
        outcome = gate.evaluate(_inp(tool_name=name, risk=risk))
        assert outcome.decision is Decision.ALLOW


def test_network_and_comms_queue_for_approval() -> None:
    gate = Gate()
    for risk, name in (
        (RiskClass.NET_READ, "net_fetch"),
        (RiskClass.NET_WRITE, "net_post"),
        (RiskClass.COMMS, "comms_send"),
        (RiskClass.IRREVERSIBLE, "rm_rf"),
    ):
        assert gate.evaluate(_inp(tool_name=name, risk=risk)).decision is Decision.APPROVE


def test_trust_rule_bypasses_approval() -> None:
    gate = Gate()
    gate.trust.add(
        agent_name=None,
        tool_name="net_fetch",
        args_matcher={"url": "https://example.com"},
        ttl_seconds=60,
    )
    # Matching args — ALLOW.
    outcome = gate.evaluate(
        _inp(
            tool_name="net_fetch",
            risk=RiskClass.NET_READ,
            args={"url": "https://example.com"},
        )
    )
    assert outcome.decision is Decision.ALLOW
    # Different URL — still APPROVE.
    outcome = gate.evaluate(
        _inp(
            tool_name="net_fetch",
            risk=RiskClass.NET_READ,
            args={"url": "https://other.example"},
        )
    )
    assert outcome.decision is Decision.APPROVE
