"""OAuthFlowManager — state bookkeeping + token exchange wiring.

The real token POST lives behind `urllib.request.urlopen`; we monkeypatch
the `_exchange_code` helper to keep the test offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.identity import AccountsStore
from core.integrations import oauth_flow as oauth_flow_mod
from core.integrations.oauth_flow import OAuthFlowManager
from core.integrations.provider import OAuthProfile, OAuthProvider, ProviderRegistry, ScopeSpec
from core.policy.risk import RiskClass


def _fake_provider() -> OAuthProvider:
    return OAuthProvider(
        name="demo",
        label="Demo",
        auth_url="https://auth.example/auth",
        token_url="https://auth.example/token",
        scope_catalog={
            "mail.send": ScopeSpec(
                name="mail.send",
                scope_uri="https://mail/send",
                label="Send",
                risk_hint=RiskClass.COMMS,
                user_voice=True,
            ),
        },
        scopes_for_role=lambda role, groups=None: ["https://mail/send"],
        profile_fetcher=lambda tokens: OAuthProfile(
            email="demo@example.com", username=None
        ),
    )


def _manager(
    tmp_path: Path,
    *,
    client_loader=None,
    setup_hint_loader=None,
) -> tuple[OAuthFlowManager, AccountsStore]:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    registry = ProviderRegistry()
    registry.register(_fake_provider())
    manager = OAuthFlowManager(
        providers=registry,
        accounts=store,
        client_loader=client_loader
        or (lambda name: ("test-client-id", "test-client-secret")),
        setup_hint_loader=setup_hint_loader,
        public_base_url="http://127.0.0.1:7424",
    )
    return manager, store


def test_start_returns_auth_url_with_state(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    out = manager.start(provider_name="demo", role="user")
    assert out["auth_url"].startswith("https://auth.example/auth?")
    assert "client_id=test-client-id" in out["auth_url"]
    assert f"state={out['state']}" in out["auth_url"]
    assert "scope=https%3A%2F%2Fmail%2Fsend" in out["auth_url"]


def test_start_rejects_unknown_provider(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    with pytest.raises(ValueError):
        manager.start(provider_name="nope", role="user")


@pytest.mark.asyncio
async def test_complete_writes_account_and_pops_state(
    tmp_path: Path, monkeypatch
) -> None:
    manager, store = _manager(tmp_path)
    started = manager.start(provider_name="demo", role="user", make_default=True)

    def _fake_exchange(**kwargs):
        assert kwargs["code"] == "code-xyz"
        return {
            "access_token": "at_1",
            "refresh_token": "rt_1",
            "scope": "https://mail/send",
            "expires_in": 3600,
            "token_type": "Bearer",
        }

    monkeypatch.setattr(oauth_flow_mod, "_exchange_code", _fake_exchange)

    account = await manager.complete(code="code-xyz", state=started["state"])
    assert account.provider == "demo"
    assert account.role == "user"
    assert account.email == "demo@example.com"
    assert store.default_id("demo", "user") == account.account_id

    # State is consumed; re-using it fails.
    with pytest.raises(ValueError):
        await manager.complete(code="code-xyz", state=started["state"])


@pytest.mark.asyncio
async def test_complete_errors_without_refresh_token(
    tmp_path: Path, monkeypatch
) -> None:
    manager, _store = _manager(tmp_path)
    started = manager.start(provider_name="demo", role="user")

    monkeypatch.setattr(
        oauth_flow_mod,
        "_exchange_code",
        lambda **_k: {"access_token": "at", "scope": ""},  # no refresh_token
    )
    with pytest.raises(RuntimeError):
        await manager.complete(code="c", state=started["state"])


def test_is_configured_reflects_client_loader(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    assert manager.is_configured("demo") is True

    no_client, _ = _manager(tmp_path, client_loader=lambda _name: None)
    assert no_client.is_configured("demo") is False


def test_start_error_includes_setup_hint(tmp_path: Path) -> None:
    manager, _ = _manager(
        tmp_path,
        client_loader=lambda _name: None,
        setup_hint_loader=lambda _name: "Set DEMO_CLIENT_ID + DEMO_CLIENT_SECRET.",
    )
    with pytest.raises(RuntimeError) as exc_info:
        manager.start(provider_name="demo", role="user")
    msg = str(exc_info.value)
    assert "no OAuth client configured for provider demo" in msg
    assert "Set DEMO_CLIENT_ID + DEMO_CLIENT_SECRET." in msg


def test_start_error_without_hint_falls_back_to_generic(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path, client_loader=lambda _name: None)
    with pytest.raises(RuntimeError) as exc_info:
        manager.start(provider_name="demo", role="user")
    assert str(exc_info.value) == "no OAuth client configured for provider demo"


def test_setup_hint_is_none_without_loader(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    assert manager.setup_hint("demo") is None
