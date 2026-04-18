"""Agent autonomy profile store + gate integration.

The store persists per-agent profiles and feeds the gate's
profile-lookup callable. Unknown or unset agents fall back to the
`assistant` default; `operator` widens NET_WRITE; `autonomous` also
widens COMMS. Invariants (financial, NEVER_WHITELISTABLE comms) are
preserved regardless of profile.
"""

from __future__ import annotations

import pytest

from core.config import get_settings
from core.db import ensure_schema
from core.policy import AgentPolicyStore, Decision, Gate, GateInput
from core.policy.risk import RiskClass


@pytest.mark.asyncio
async def test_store_persists_and_hydrates() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)

    s1 = AgentPolicyStore(settings.db_path)
    await s1.hydrate()
    assert s1.get("sales_bot") == "assistant"

    await s1.set("sales_bot", "operator")
    assert s1.get("sales_bot") == "operator"

    s2 = AgentPolicyStore(settings.db_path)
    await s2.hydrate()
    assert s2.get("sales_bot") == "operator"
    assert s2.get("nobody") == "assistant"


@pytest.mark.asyncio
async def test_invalid_profile_rejected() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)

    store = AgentPolicyStore(settings.db_path)
    await store.hydrate()
    with pytest.raises(ValueError):
        await store.set("x", "god-mode")


@pytest.mark.asyncio
async def test_gate_uses_store_lookup_end_to_end() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)

    store = AgentPolicyStore(settings.db_path)
    await store.hydrate()
    await store.set("sdr", "autonomous")
    await store.set("intern", "observer")

    gate = Gate(agent_profile_lookup=store.get)

    def _inp(**kw) -> GateInput:
        return GateInput(tool_name="net_post", risk=RiskClass.NET_WRITE, **kw)

    # sdr (autonomous) — NET_WRITE auto-allowed.
    assert gate.evaluate(_inp(agent_name="sdr")).decision is Decision.ALLOW
    # intern (observer) and free chat — NET_WRITE still approval-gated.
    assert gate.evaluate(_inp(agent_name="intern")).decision is Decision.APPROVE
    assert gate.evaluate(_inp(agent_name=None)).decision is Decision.APPROVE

    # FINANCIAL stays approval-gated regardless of profile — the
    # financial sub-policy hard-codes fresh approvals for deposit /
    # withdraw / transfer even under the autonomous profile.
    fin = GateInput(
        tool_name="finance_deposit",
        risk=RiskClass.FINANCIAL,
        agent_name="sdr",
    )
    outcome = gate.evaluate(fin)
    assert outcome.decision is Decision.APPROVE
    assert outcome.bypass_trust is True
