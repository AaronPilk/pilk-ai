"""User-managed API keys for external integrations.

Single source of truth for third-party credentials that the operator
pastes into the dashboard at runtime — HubSpot, Hunter.io, Google Cloud
(Places + PageSpeed), etc. Lives in the same SQLite file as every other
daemon-local table; the row count is small enough that pragma tweaks
don't matter.

Lookup order when a tool needs a key:

    1. DB row written via Settings → API Keys          ← user override
    2. Env-var fallback (PILK_*/*_API_KEY in settings) ← Railway / local dev
    3. None → tool surfaces a friendly "not configured" error

Phase 2 adds per-user scoping: a ``user_id`` column on this table plus
a move to Supabase so each signed-in operator brings their own. The
single-row `name` PK today makes that migration a straight ``ALTER
TABLE`` + key rewrite.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class SecretEntry:
    name: str
    updated_at: str
    # `value` is only exposed via get_value(); list/summary APIs never
    # leak it. Keeping the field here for in-process use.
    value: str


class IntegrationSecretsStore:
    """Thin CRUD layer over the ``integration_secrets`` table.

    Every write is synchronous + autocommit — these rows change rarely
    (maybe once per API key rotation) and the value being fresh matters
    more than throughput.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        # Each call opens a short-lived connection. SQLite handles its
        # own write serialization; we never hold the connection across
        # awaits so no lock contention with the orchestrator loop.
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def get_value(self, name: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM integration_secrets WHERE name = ?",
                (name,),
            ).fetchone()
        return row[0] if row else None

    def list_entries(self) -> list[SecretEntry]:
        """Every configured secret, most recently updated first.

        Used by the dashboard to render 'configured ✓' badges next to
        each integration. ``value`` is included in the return type but
        the HTTP route strips it before responding — plain rule is: the
        store returns the truth, the transport decides what's public.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name, value, updated_at FROM integration_secrets "
                "ORDER BY updated_at DESC"
            ).fetchall()
        return [SecretEntry(name=r[0], value=r[1], updated_at=r[2]) for r in rows]

    def upsert(self, name: str, value: str) -> None:
        if not name:
            raise ValueError("secret name is required")
        if value == "":
            raise ValueError("empty values are not allowed — use delete() instead")
        now = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO integration_secrets(name, value, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (name, value, now),
            )
            conn.commit()

    def delete(self, name: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM integration_secrets WHERE name = ?", (name,)
            )
            conn.commit()
            return cur.rowcount > 0


# ── Process-wide singleton wiring ─────────────────────────────────
#
# Tools that don't own a factory closure (see core/tools/builtin/
# sales_ops.py) need a module-level way to reach the live store. The
# lifespan of ``core.api.app`` calls ``set_integration_secrets_store``
# once on boot; callers read via ``resolve_secret``.

_store: IntegrationSecretsStore | None = None


def set_integration_secrets_store(store: IntegrationSecretsStore | None) -> None:
    global _store
    _store = store


def get_integration_secrets_store() -> IntegrationSecretsStore | None:
    return _store


def resolve_secret(name: str, env_fallback: str | None) -> str | None:
    """Return the live value for a named secret, or None if unconfigured.

    Dashboard override wins over env-var fallback. Callers usually pass
    the current Pydantic-settings value as the fallback so local-dev
    and Railway env vars still work when no row has been written yet.

    SQLite errors (db file missing, schema not yet migrated) fall
    through silently to the env fallback — we'd rather the tool run
    against the env-configured key than crash every request because the
    store is in a weird state at boot.
    """
    if _store is not None:
        try:
            live = _store.get_value(name)
        except sqlite3.Error:
            live = None
        if live:
            return live
    return env_fallback
