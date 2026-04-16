from core.policy import Decision, Gate
from core.policy.risk import RiskClass


def test_low_risk_classes_auto_allow() -> None:
    gate = Gate()
    for risk in (RiskClass.READ, RiskClass.WRITE_LOCAL, RiskClass.EXEC_LOCAL):
        assert gate.evaluate(risk).decision is Decision.ALLOW


def test_network_writes_are_rejected_until_batch_3() -> None:
    gate = Gate()
    assert gate.evaluate(RiskClass.NET_WRITE).decision is Decision.REJECT
    assert gate.evaluate(RiskClass.COMMS).decision is Decision.REJECT
    assert gate.evaluate(RiskClass.FINANCIAL).decision is Decision.REJECT
    assert gate.evaluate(RiskClass.IRREVERSIBLE).decision is Decision.REJECT
