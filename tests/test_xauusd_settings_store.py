"""Runtime-settings store + execution_mode helper tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db.migrations import ensure_schema
from core.trading.xauusd.settings_store import (
    DEFAULT_EXECUTION_MODE,
    EXECUTION_MODES,
    XAUUSDSettingsStore,
    get_execution_mode,
    get_xauusd_settings_store,
    set_execution_mode,
    set_xauusd_settings_store,
)


@pytest.fixture
def store(tmp_path: Path) -> XAUUSDSettingsStore:
    db_path = tmp_path / "pilk.db"
    ensure_schema(db_path)
    s = XAUUSDSettingsStore(db_path)
    set_xauusd_settings_store(s)
    yield s
    # Tear down process-local pointer so later tests don't see this DB.
    set_xauusd_settings_store(None)


def test_known_modes_exactly_two() -> None:
    assert frozenset({"approve", "autonomous"}) == EXECUTION_MODES


def test_default_when_store_unset() -> None:
    set_xauusd_settings_store(None)
    assert get_execution_mode() == DEFAULT_EXECUTION_MODE == "approve"


def test_round_trip(store: XAUUSDSettingsStore) -> None:
    assert get_execution_mode() == "approve"
    assert set_execution_mode("autonomous") == "autonomous"
    assert get_execution_mode() == "autonomous"
    # Raw DB read as well.
    assert store.get("execution_mode") == "autonomous"


def test_set_normalizes_whitespace_and_case(store: XAUUSDSettingsStore) -> None:
    assert set_execution_mode("  AUTONOMOUS  ") == "autonomous"
    assert store.get("execution_mode") == "autonomous"


def test_set_rejects_unknown(store: XAUUSDSettingsStore) -> None:
    with pytest.raises(ValueError, match="unknown execution_mode"):
        set_execution_mode("paper-only")


def test_set_without_store_raises() -> None:
    set_xauusd_settings_store(None)
    with pytest.raises(RuntimeError, match="not initialized"):
        set_execution_mode("autonomous")


def test_get_falls_back_on_stale_value(
    store: XAUUSDSettingsStore,
) -> None:
    # Simulate a mode the code no longer recognizes (downgrade scenario).
    store.upsert("execution_mode", "experimental")
    assert get_execution_mode() == DEFAULT_EXECUTION_MODE


def test_upsert_twice_updates_value(store: XAUUSDSettingsStore) -> None:
    store.upsert("execution_mode", "approve")
    store.upsert("execution_mode", "autonomous")
    entries = store.list_entries()
    assert len(entries) == 1
    assert entries[0].value == "autonomous"


def test_delete_reverts_to_default(store: XAUUSDSettingsStore) -> None:
    set_execution_mode("autonomous")
    store.delete("execution_mode")
    assert get_execution_mode() == DEFAULT_EXECUTION_MODE


def test_accessor_returns_live_store(store: XAUUSDSettingsStore) -> None:
    assert get_xauusd_settings_store() is store
