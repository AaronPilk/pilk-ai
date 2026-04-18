"""X (Twitter) provider + post tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.identity import AccountsStore
from core.integrations import oauth_flow as oauth_flow_mod
from core.integrations.oauth_flow import OAuthFlowManager
from core.integrations.provider import ProviderRegistry
from core.integrations.providers.x import SCOPE_CATALOG, x_provider
from core.integrations.x import make_x_tools
from core.policy.comms import NEVER_WHITELISTABLE
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext


def test_x_default_group_requests_post_scopes() -> None:
    scopes = set(x_provider.scopes_for_role("user", None))
    assert SCOPE_CATALOG["tweet.write"].scope_uri in scopes
    assert SCOPE_CATALOG["tweet.read"].scope_uri in scopes
    assert SCOPE_CATALOG["users.read"].scope_uri in scopes
    assert SCOPE_CATALOG["offline.access"].scope_uri in scopes


def test_x_uses_pkce_and_basic_auth_and_refresh() -> None:
    assert x_provider.uses_pkce is True
    assert x_provider.token_exchange_mode == "basic"
    # offline.access means X will return a refresh token.
    assert x_provider.requires_refresh_token is True


def test_x_post_tool_metadata(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    [post] = make_x_tools(store)
    assert post.name == "x_post_as_me"
    assert post.risk == RiskClass.COMMS
    assert post.account_binding is not None
    assert post.account_binding.provider == "x"
    assert post.account_binding.role == "user"


def test_x_post_is_never_trust_whitelistable() -> None:
    assert "x_post_as_me" in NEVER_WHITELISTABLE


@pytest.mark.asyncio
async def test_x_refuses_when_not_linked(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    [post] = make_x_tools(store)
    result = await post.handler({"text": "hello"}, ToolContext())
    assert result.is_error is True


@pytest.mark.asyncio
async def test_x_refuses_over_limit_tweet(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    # Seed a fake X account so we get past the not-linked check first.
    from core.identity.accounts import OAuthTokens

    store.upsert(
        provider="x",
        role="user",
        label="X",
        email="@test",
        username="test",
        scopes=["tweet.write"],
        tokens=OAuthTokens(
            access_token="at",
            refresh_token="rt",
            client_id="cid",
            client_secret="cs",
            scopes=["tweet.write"],
        ),
        make_default=True,
    )
    [post] = make_x_tools(store)
    result = await post.handler({"text": "x" * 300}, ToolContext())
    assert result.is_error is True
    assert "280" in result.content


def test_x_oauth_start_includes_pkce_challenge(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    registry = ProviderRegistry()
    registry.register(x_provider)
    manager = OAuthFlowManager(
        providers=registry,
        accounts=store,
        client_loader=lambda _n: ("x-client", "x-secret"),
        public_base_url="http://127.0.0.1:7424",
    )
    out = manager.start(provider_name="x", role="user")
    assert "code_challenge=" in out["auth_url"]
    assert "code_challenge_method=S256" in out["auth_url"]


@pytest.mark.asyncio
async def test_x_complete_uses_basic_auth_and_pkce_verifier(
    tmp_path: Path, monkeypatch
) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    registry = ProviderRegistry()
    registry.register(x_provider)
    manager = OAuthFlowManager(
        providers=registry,
        accounts=store,
        client_loader=lambda _n: ("x-client", "x-secret"),
        public_base_url="http://127.0.0.1:7424",
    )
    started = manager.start(provider_name="x", role="user", make_default=True)

    captured: dict = {}

    def _fake_exchange(**kwargs):
        captured.update(kwargs)
        return {
            "access_token": "xat",
            "refresh_token": "xrt",
            "scope": "tweet.write tweet.read users.read offline.access",
            "token_type": "bearer",
            "expires_in": 7200,
        }

    def _fake_profile(_tokens):
        from core.integrations.provider import OAuthProfile

        return OAuthProfile(email="@test", username="test")

    monkeypatch.setattr(oauth_flow_mod, "_exchange_code", _fake_exchange)
    monkeypatch.setattr(x_provider, "profile_fetcher", _fake_profile)

    account = await manager.complete(code="the-code", state=started["state"])
    assert account.provider == "x"
    assert captured["mode"] == "basic"
    assert captured["code_verifier"] is not None
    assert len(captured["code_verifier"]) >= 43  # RFC 7636 minimum


def test_basic_mode_builds_authorization_header(monkeypatch) -> None:
    import base64

    captured: dict = {}

    def _fake_urlopen(req, timeout=20):
        del timeout
        captured["headers"] = dict(req.headers)
        captured["body"] = req.data.decode("utf-8")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"access_token":"t","refresh_token":"r","scope":"a"}'

        return _Resp()

    monkeypatch.setattr(oauth_flow_mod.urllib.request, "urlopen", _fake_urlopen)
    oauth_flow_mod._exchange_code(
        token_url="https://example/token",
        code="c",
        client_id="cid",
        client_secret="csec",
        redirect_uri="http://cb",
        code_verifier="v",
        mode="basic",
    )
    expected = "Basic " + base64.b64encode(b"cid:csec").decode("ascii")
    assert captured["headers"]["Authorization"] == expected
    # client_id still in body for X, client_secret NOT in body
    assert "client_id=cid" in captured["body"]
    assert "client_secret" not in captured["body"]
    assert "code_verifier=v" in captured["body"]
