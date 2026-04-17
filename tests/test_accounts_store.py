"""AccountsStore CRUD + default bookkeeping + secrets."""

from __future__ import annotations

from pathlib import Path

from core.identity import AccountBinding, AccountsStore, account_id_for
from core.identity.accounts import OAuthTokens


def _tokens(refresh: str = "rf_abc") -> OAuthTokens:
    return OAuthTokens(
        access_token="at_1",
        refresh_token=refresh,
        client_id="cid",
        client_secret="csec",
        scopes=["https://mail/send"],
    )


def test_upsert_list_get(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()

    account = store.upsert(
        provider="google",
        role="user",
        label="Work",
        email="aaron@work.com",
        username=None,
        scopes=["gmail.send"],
        tokens=_tokens(),
    )
    assert account.account_id == account_id_for("google", "user", "aaron@work.com")

    fetched = store.get(account.account_id)
    assert fetched is not None
    assert fetched.email == "aaron@work.com"

    listed = store.list(provider="google", role="user")
    assert [a.account_id for a in listed] == [account.account_id]


def test_first_upsert_sets_default(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    a = store.upsert(
        provider="google",
        role="user",
        label="Work",
        email="a@x.com",
        username=None,
        scopes=[],
        tokens=_tokens(),
    )
    assert store.default_id("google", "user") == a.account_id


def test_second_upsert_does_not_steal_default(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    first = store.upsert(
        provider="google",
        role="user",
        label="Work",
        email="a@x.com",
        username=None,
        scopes=[],
        tokens=_tokens("rf_1"),
    )
    second = store.upsert(
        provider="google",
        role="user",
        label="Personal",
        email="b@y.com",
        username=None,
        scopes=[],
        tokens=_tokens("rf_2"),
    )
    assert store.default_id("google", "user") == first.account_id
    assert store.get(second.account_id) is not None


def test_set_default_and_remove_clears_default(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    a = store.upsert(
        provider="google",
        role="user",
        label="Work",
        email="a@x.com",
        username=None,
        scopes=[],
        tokens=_tokens("rf_1"),
    )
    b = store.upsert(
        provider="google",
        role="user",
        label="Personal",
        email="b@y.com",
        username=None,
        scopes=[],
        tokens=_tokens("rf_2"),
    )
    assert store.set_default(b.account_id)
    assert store.default_id("google", "user") == b.account_id

    assert store.remove(b.account_id)
    # default cleared since b was the current default
    assert store.default_id("google", "user") is None
    # a still around
    assert store.get(a.account_id) is not None


def test_resolve_binding_and_load_tokens(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    a = store.upsert(
        provider="google",
        role="user",
        label="Work",
        email="a@x.com",
        username=None,
        scopes=[],
        tokens=_tokens("rf_1"),
    )
    resolved = store.resolve_binding(AccountBinding(provider="google", role="user"))
    assert resolved is not None and resolved.account_id == a.account_id

    tokens = store.load_tokens(a.account_id)
    assert tokens is not None and tokens.refresh_token == "rf_1"

    # Secrets file is 0600 on platforms that honor it.
    secret_path = tmp_path / "identity" / "accounts" / "secrets" / f"{a.account_id}.json"
    assert secret_path.exists()
