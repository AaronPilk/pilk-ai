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

CURRENT_VERSION = 2
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
