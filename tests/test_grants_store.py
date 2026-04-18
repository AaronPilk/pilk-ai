"""GrantsStore — permissive fallback + explicit opt-in semantics."""

from __future__ import annotations

from pathlib import Path

from core.identity import GrantsStore


def test_unknown_agent_is_permissive(tmp_path: Path) -> None:
    store = GrantsStore(tmp_path)
    assert store.allows("never_registered", "google-user-aaron") is True


def test_register_agent_with_empty_list_denies_everything(tmp_path: Path) -> None:
    store = GrantsStore(tmp_path)
    store.register_agent("new_agent")
    assert store.has_entry("new_agent") is True
    assert store.allows("new_agent", "google-user-aaron") is False


def test_grant_and_revoke_roundtrip(tmp_path: Path) -> None:
    store = GrantsStore(tmp_path)
    store.register_agent("outreach")
    assert store.grant("outreach", "google-user-aaron") is True
    assert store.allows("outreach", "google-user-aaron") is True
    assert store.agents_for("google-user-aaron") == ["outreach"]
    # Second grant of the same pair is a no-op.
    assert store.grant("outreach", "google-user-aaron") is False
    assert store.revoke("outreach", "google-user-aaron") is True
    assert store.allows("outreach", "google-user-aaron") is False
    # Revoke of an unknown pair is a no-op.
    assert store.revoke("outreach", "doesnt-exist") is False


def test_grant_creates_entry_for_unknown_agent(tmp_path: Path) -> None:
    store = GrantsStore(tmp_path)
    # Not pre-registered — grant still works and creates a restrictive entry.
    assert store.grant("brand_new", "google-user-aaron") is True
    assert store.has_entry("brand_new") is True
    assert store.accounts_for("brand_new") == ["google-user-aaron"]


def test_remove_agent_returns_it_to_permissive(tmp_path: Path) -> None:
    store = GrantsStore(tmp_path)
    store.register_agent("x", accounts=["google-user-aaron"])
    assert store.allows("x", "something-else") is False
    store.remove_agent("x")
    assert store.has_entry("x") is False
    assert store.allows("x", "something-else") is True


def test_remove_account_everywhere_clears_from_all_grants(tmp_path: Path) -> None:
    store = GrantsStore(tmp_path)
    store.grant("a1", "google-user-aaron")
    store.grant("a2", "google-user-aaron")
    store.grant("a2", "google-user-other")
    changed = store.remove_account_everywhere("google-user-aaron")
    assert changed == 2
    assert store.accounts_for("a1") == []
    assert store.accounts_for("a2") == ["google-user-other"]


def test_persistence_across_instances(tmp_path: Path) -> None:
    store = GrantsStore(tmp_path)
    store.grant("outreach", "google-user-aaron")
    # New instance reads from the same file.
    store2 = GrantsStore(tmp_path)
    assert store2.allows("outreach", "google-user-aaron") is True
