"""Slack provider — scope groups, token extraction, auth param shape."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.identity import AccountsStore
from core.identity.accounts import OAuthTokens
from core.integrations import oauth_flow as oauth_flow_mod
from core.integrations.oauth_flow import OAuthFlowManager
from core.integrations.provider import ProviderRegistry
from core.integrations.providers.slack import (
    SCOPE_CATALOG,
    _token_extractor,
    slack_provider,
)
from core.integrations.slack import make_slack_tools
from core.policy.comms import NEVER_WHITELISTABLE
from core.policy.gate import Decision, Gate, GateInput
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext


def test_slack_defaults_messages_group() -> None:
    scopes = set(slack_provider.scopes_for_role("user", None))
    assert SCOPE_CATALOG["chat.write"].scope_uri in scopes
    assert SCOPE_CATALOG["channels.read"].scope_uri not in scopes


def test_slack_channels_group_adds_channels_read() -> None:
    scopes = set(
        slack_provider.scopes_for_role("user", ["messages", "channels"])
    )
    assert SCOPE_CATALOG["chat.write"].scope_uri in scopes
    assert SCOPE_CATALOG["channels.read"].scope_uri in scopes


def test_slack_uses_user_scope_param_and_no_refresh_token() -> None:
    assert slack_provider.scope_param_name == "user_scope"
    assert slack_provider.requires_refresh_token is False


def test_token_extractor_pulls_user_token_from_authed_user() -> None:
    raw = {
        "ok": True,
        "access_token": "xoxb-bot",
        "scope": "channels:read",
        "authed_user": {
            "id": "U123",
            "access_token": "xoxp-user",
            "scope": "chat:write",
            "token_type": "Bearer",
        },
        "team": {"id": "T1", "name": "Acme"},
    }
    out = _token_extractor(raw)
    assert out["access_token"] == "xoxp-user"
    assert out["scope"] == "chat:write"
    assert out["refresh_token"] == ""


def test_token_extractor_raises_on_not_ok() -> None:
    with pytest.raises(RuntimeError):
        _token_extractor({"ok": False, "error": "invalid_code"})


def test_slack_post_tool_is_comms_and_bound(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    [post] = make_slack_tools(store)
    assert post.name == "slack_post_as_me"
    assert post.risk == RiskClass.COMMS
    assert post.account_binding is not None
    assert post.account_binding.provider == "slack"
    assert post.account_binding.role == "user"


def test_slack_post_is_never_trust_whitelistable() -> None:
    assert "slack_post_as_me" in NEVER_WHITELISTABLE


def test_slack_post_bypasses_trust_rules() -> None:
    # Mirrors the gmail_send_as_me comms check.
    gate = Gate()
    outcome = gate.evaluate(
        GateInput(
            tool_name="slack_post_as_me",
            risk=RiskClass.COMMS,
            args={"channel": "#general", "text": "hi"},
        )
    )
    assert outcome.decision == Decision.APPROVE
    assert outcome.bypass_trust is True


@pytest.mark.asyncio
async def test_slack_tool_refuses_when_not_linked(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    [post] = make_slack_tools(store)
    result = await post.handler(
        {"channel": "#general", "text": "hello"}, ToolContext()
    )
    assert result.is_error is True
    assert "Connected accounts" in result.content


@pytest.mark.asyncio
async def test_slack_oauth_flow_completes_without_refresh_token(
    tmp_path: Path, monkeypatch
) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    registry = ProviderRegistry()
    registry.register(slack_provider)
    manager = OAuthFlowManager(
        providers=registry,
        accounts=store,
        client_loader=lambda _n: ("slack-client-id", "slack-client-secret"),
        public_base_url="http://127.0.0.1:7424",
    )
    started = manager.start(provider_name="slack", role="user", make_default=True)
    assert "user_scope=chat%3Awrite" in started["auth_url"]

    def _fake_exchange(**_kwargs):
        return {
            "ok": True,
            "access_token": "xoxb-bot",
            "scope": "chat:write",
            "authed_user": {
                "id": "U123",
                "access_token": "xoxp-user",
                "scope": "chat:write",
                "token_type": "Bearer",
            },
            "team": {"id": "T1", "name": "Acme"},
        }

    monkeypatch.setattr(oauth_flow_mod, "_exchange_code", _fake_exchange)

    # Patch the profile fetcher so the test doesn't hit auth.test.
    def _fake_profile(_tokens):
        from core.integrations.provider import OAuthProfile

        return OAuthProfile(email="aaron@Acme", username="aaron")

    monkeypatch.setattr(slack_provider, "profile_fetcher", _fake_profile)

    account = await manager.complete(code="code-xyz", state=started["state"])
    assert account.provider == "slack"
    assert account.role == "user"
    assert account.username == "aaron"
    assert store.default_id("slack", "user") == account.account_id

    # Refresh token stayed empty — Slack doesn't hand one out by default.
    secret_tokens: OAuthTokens = store.load_tokens(account.account_id)
    assert secret_tokens is not None
    assert secret_tokens.refresh_token == ""
    assert secret_tokens.access_token == "xoxp-user"
