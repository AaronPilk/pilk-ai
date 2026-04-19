"""Runtime-editable settings for the XAUUSD execution agent.

Separate from ``integration_secrets`` because these aren't secrets —
they're operator toggles the UI renders as switches. Single-tenant KV
store today; Phase 2 re-keys on ``user_id`` the same way the rest of the
per-user data migrates.

The only setting today is ``execution_mode``:

    approve      — every trade decision is queued for the operator to
                   approve via the existing ApprovalManager before the
                   broker tool runs. Safe default.
    autonomous   — the agent places orders within its risk caps without
                   per-trade approval. Operator flips this from the UI
                   once they've watched enough live decisions to trust
                   the model.

Future knobs likely to live here: ``allow_countertrend``, ``cooldown_minutes``,
``disabled_until`` (sticky pause), ``paper_trade_notional_cap``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

EXECUTION_MODES: frozenset[str] = frozenset({"approve", "autonomous"})
DEFAULT_EXECUTION_MODE: str = "approve"


@dataclass(frozen=True)
class SettingEntry:
    name: str
    value: str
    updated_at: str


class XAUUSDSettingsStore:
    """Thin CRUD over the ``xauusd_settings`` table.

    Every call opens a short-lived connection; rows here change at
    most a handful of times per day so throughput is a non-concern.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def get(self, name: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM xauusd_settings WHERE name = ?", (name,)
            ).fetchone()
        return row[0] if row else None

    def upsert(self, name: str, value: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO xauusd_settings(name, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       value = excluded.value,
                       updated_at = excluded.updated_at""",
                (name, value, now),
            )
            conn.commit()

    def delete(self, name: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM xauusd_settings WHERE name = ?", (name,)
            )
            conn.commit()
            return cur.rowcount > 0

    def list_entries(self) -> list[SettingEntry]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name, value, updated_at FROM xauusd_settings "
                "ORDER BY name"
            ).fetchall()
        return [SettingEntry(name=r[0], value=r[1], updated_at=r[2]) for r in rows]


# ── Process-wide singleton ────────────────────────────────────────
#
# Same pattern as integration_secrets: FastAPI lifespan wires a live
# store once at boot and the tool layer reads through this accessor
# instead of threading app-state through every call-site.

_store: XAUUSDSettingsStore | None = None


def set_xauusd_settings_store(store: XAUUSDSettingsStore | None) -> None:
    global _store
    _store = store


def get_xauusd_settings_store() -> XAUUSDSettingsStore | None:
    return _store


def get_execution_mode(default: str = DEFAULT_EXECUTION_MODE) -> str:
    """Return the active execution mode. Falls back to ``default``.

    SQLite errors (db missing, table not migrated) degrade silently so
    the agent still works in a fresh test environment or boot sequence
    where the store hasn't been wired yet.
    """
    if _store is None:
        return default
    try:
        raw = _store.get("execution_mode")
    except sqlite3.Error:
        return default
    if raw is None:
        return default
    mode = raw.strip().lower()
    if mode not in EXECUTION_MODES:
        return default
    return mode


def set_execution_mode(mode: str) -> str:
    """Persist ``mode`` and return the normalized value.

    Raises ``ValueError`` for unknown modes so the API layer can map to
    a 400, and crashes loudly in test/dev if the store isn't wired.
    """
    normalized = mode.strip().lower()
    if normalized not in EXECUTION_MODES:
        raise ValueError(
            f"unknown execution_mode '{mode}'. Known: {sorted(EXECUTION_MODES)}"
        )
    if _store is None:
        raise RuntimeError("xauusd_settings store is not initialized")
    _store.upsert("execution_mode", normalized)
    return normalized


__all__ = [
    "DEFAULT_EXECUTION_MODE",
    "EXECUTION_MODES",
    "SettingEntry",
    "XAUUSDSettingsStore",
    "get_execution_mode",
    "get_xauusd_settings_store",
    "set_execution_mode",
    "set_xauusd_settings_store",
]
