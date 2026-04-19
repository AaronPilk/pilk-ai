"""Applies `schema.sql` and versioned migrations to the PILK SQLite database.

The base `schema.sql` is idempotent — re-running it on startup is safe.
When a backward-incompatible change is needed, add an entry to `MIGRATIONS`
keyed by the new version number and a list of SQL statements. Everything
runs in one transaction per version bump.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

CURRENT_VERSION = 5
SCHEMA_FILE = Path(__file__).parent / "schema.sql"


MIGRATIONS: dict[int, list[str]] = {
    # v2: drop the agent_name FK on sandboxes. A sandbox's lifecycle is
    # decoupled from agent registration — a sandbox can outlive or precede
    # the agent row. SQLite can't drop a column constraint, so we recreate
    # the table. The table is only used transiently across sessions; data
    # loss here is expected and safe.
    2: [
        "DROP TABLE IF EXISTS sandboxes",
        """CREATE TABLE sandboxes (
            id            TEXT PRIMARY KEY,
            type          TEXT NOT NULL,
            agent_name    TEXT,
            state         TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            destroyed_at  TEXT,
            metadata_json TEXT
        )""",
    ],
    # v3: trust_audit table for the approval/trust layer (batch 3). The
    # live trust store is in-memory; this table is the historical mirror.
    3: [
        """CREATE TABLE IF NOT EXISTS trust_audit (
            id            TEXT PRIMARY KEY,
            agent_name    TEXT,
            tool_name     TEXT NOT NULL,
            args_json     TEXT,
            ttl_seconds   INTEGER NOT NULL,
            expires_at    TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            created_by    TEXT NOT NULL DEFAULT 'user',
            reason        TEXT,
            approval_id   TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_trust_audit_created ON trust_audit(created_at)",
    ],
    # v4: structured memory. What PILK is currently retaining — explicit
    # preferences, standing instructions, remembered facts, observed
    # patterns. Entries are user-curated in this phase; auto-extraction
    # and vector recall are deferred.
    4: [
        """CREATE TABLE IF NOT EXISTS memory_entries (
            id          TEXT PRIMARY KEY,
            kind        TEXT NOT NULL,
            title       TEXT NOT NULL,
            body        TEXT NOT NULL DEFAULT '',
            source      TEXT NOT NULL DEFAULT 'user',
            plan_id     TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory_entries(kind)",
        "CREATE INDEX IF NOT EXISTS idx_memory_created ON memory_entries(created_at)",
    ],
    # v5: per-agent autonomy profile. Persisted so the gate can widen
    # the auto-allow set for trusted agents across restarts.
    5: [
        """CREATE TABLE IF NOT EXISTS agent_policies (
            agent_name TEXT PRIMARY KEY,
            profile    TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
    ],
    # v6: user-managed API keys for external integrations (HubSpot,
    # Hunter.io, Google APIs, etc.). One row per logical secret name;
    # values are plaintext under 0600 OS perms on the SQLite file
    # (same security boundary as OAuth tokens in accounts/secrets/).
    # Phase 2 moves this table (and the OAuth blob) onto Supabase with
    # per-user scoping; single-tenant v1 keeps it alongside the daemon.
    6: [
        """CREATE TABLE IF NOT EXISTS integration_secrets (
            name       TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
    ],
}


def ensure_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        row = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        current = row[0] if row and row[0] is not None else 0
        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            for stmt in MIGRATIONS[version]:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )
        if current < 1:
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (1, datetime.now(UTC).isoformat()),
            )
        conn.commit()
    finally:
        conn.close()
