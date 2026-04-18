"""Gateway layer 0: agent x account grants are enforced before policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.identity import AccountsStore, GrantsStore
from core.identity.accounts import OAuthTokens
from core.policy import Gate
from core.policy.risk import RiskClass
from core.tools import Gateway, ToolRegistry
from core.tools.registry import (
    AccountBinding,
    Tool,
    ToolContext,
    ToolOutcome,
)


def _make_store(tmp_path: Path) -> AccountsStore:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    store.upsert(
        provider="google",
        role="user",
        label="Work",
        email="aaron@work.com",
        username=None,
        scopes=["gmail.readonly"],
        tokens=OAuthTokens(
            access_token="at",
            refresh_token="rt",
            client_id="cid",
            client_secret="cs",
            scopes=["gmail.readonly"],
        ),
        make_default=True,
    )
    return store


def _make_tool() -> Tool:
    async def _handler(args: dict, ctx: ToolContext) -> ToolOutcome:
        return ToolOutcome(content="ran")

    # Use READ so the policy gate auto-allows past layer 0 — this test
    # is about access grants, not approvals.
    return Tool(
        name="gmail_read_me",
        description="read",
        input_schema={"type": "object"},
        risk=RiskClass.READ,
        handler=_handler,
        account_binding=AccountBinding(provider="google", role="user"),
    )


def _wired_gateway(tmp_path: Path):
    accounts = _make_store(tmp_path)
    grants = GrantsStore(tmp_path)
    registry = ToolRegistry()
    registry.register(_make_tool())
    gate = Gate()
    gateway = Gateway(
        registry,
        gate,
        approvals=None,
        accounts=accounts,
        grants=grants,
    )
    return gateway, accounts, grants


@pytest.mark.asyncio
async def test_top_level_chat_bypasses_grants(tmp_path: Path) -> None:
    gateway, _, _ = _wired_gateway(tmp_path)
    result = await gateway.execute(
        "gmail_read_me", {}, ToolContext(agent_name=None)
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_unknown_agent_is_permissive(tmp_path: Path) -> None:
    gateway, _, _ = _wired_gateway(tmp_path)
    result = await gateway.execute(
        "gmail_read_me", {}, ToolContext(agent_name="never_registered")
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_registered_agent_without_grant_is_denied(tmp_path: Path) -> None:
    gateway, _, grants = _wired_gateway(tmp_path)
    grants.register_agent("triage_agent")  # explicit entry, empty accounts
    result = await gateway.execute(
        "gmail_read_me", {}, ToolContext(agent_name="triage_agent")
    )
    assert result.ok is False
    assert result.rejection_reason is not None
    assert "access_denied" in result.rejection_reason


@pytest.mark.asyncio
async def test_granted_agent_passes_layer_0(tmp_path: Path) -> None:
    gateway, accounts, grants = _wired_gateway(tmp_path)
    default = accounts.default("google", "user")
    assert default is not None
    grants.register_agent("triage_agent")
    grants.grant("triage_agent", default.account_id)
    result = await gateway.execute(
        "gmail_read_me", {}, ToolContext(agent_name="triage_agent")
    )
    assert result.ok is True
