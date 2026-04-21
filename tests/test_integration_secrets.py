"""Unit tests for the integration-secrets store and resolver.

Covers:
- Upsert + get roundtrip on a real SQLite file (via the shared conftest
  fixture that isolates PILK_HOME per test).
- Update overwrites a previous value; delete returns True/False based
  on whether a row existed.
- ``resolve_secret`` picks the store value over the env fallback; empty
  stored value lets the fallback through.
- Module singleton is set/unset cleanly by tests (no cross-test leakage).
"""

from __future__ import annotations

import pytest

from core.config import get_settings
from core.db import ensure_schema
from core.secrets import (
    IntegrationSecretsStore,
    get_integration_secrets_store,
    resolve_secret,
    set_integration_secrets_store,
)


@pytest.fixture
def store() -> IntegrationSecretsStore:
    """Fresh SQLite + registered singleton for every test."""
    settings = get_settings()
    ensure_schema(settings.db_path)
    s = IntegrationSecretsStore(settings.db_path)
    set_integration_secrets_store(s)
    yield s
    set_integration_secrets_store(None)


def test_upsert_and_get(store: IntegrationSecretsStore) -> None:
    assert store.get_value("ghl_api_key") is None
    store.upsert("ghl_api_key", "tok-1")
    assert store.get_value("ghl_api_key") == "tok-1"


def test_upsert_replaces(store: IntegrationSecretsStore) -> None:
    store.upsert("ghl_api_key", "tok-1")
    store.upsert("ghl_api_key", "tok-2")
    assert store.get_value("ghl_api_key") == "tok-2"


def test_upsert_rejects_empty_value(store: IntegrationSecretsStore) -> None:
    with pytest.raises(ValueError, match="empty values"):
        store.upsert("ghl_api_key", "")


def test_delete_roundtrip(store: IntegrationSecretsStore) -> None:
    store.upsert("ghl_api_key", "tok-1")
    assert store.delete("ghl_api_key") is True
    assert store.get_value("ghl_api_key") is None
    # Deleting again is a no-op, not an error.
    assert store.delete("ghl_api_key") is False


def test_list_entries(store: IntegrationSecretsStore) -> None:
    store.upsert("hunter_io_api_key", "h-1")
    store.upsert("ghl_api_key", "t-1")
    names = sorted(e.name for e in store.list_entries())
    assert names == ["ghl_api_key", "hunter_io_api_key"]


def test_resolve_secret_prefers_store(store: IntegrationSecretsStore) -> None:
    store.upsert("ghl_api_key", "live")
    assert (
        resolve_secret("ghl_api_key", "env-fallback") == "live"
    )


def test_resolve_secret_falls_back_to_env(
    store: IntegrationSecretsStore,
) -> None:
    assert (
        resolve_secret("ghl_api_key", "env-fallback")
        == "env-fallback"
    )


def test_resolve_secret_handles_no_store() -> None:
    # Explicitly clear; this tests the "boot hasn't run yet" branch.
    set_integration_secrets_store(None)
    assert get_integration_secrets_store() is None
    assert resolve_secret("anything", "fallback") == "fallback"
    assert resolve_secret("anything", None) is None
