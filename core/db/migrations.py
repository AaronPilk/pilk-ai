"""Applies `schema.sql` to the PILK SQLite database and records the version.

Idempotent: safe to call on every startup. Future migrations append new
`.sql` files under `core/db/migrations/` and bump `CURRENT_VERSION`.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

CURRENT_VERSION = 1
SCHEMA_FILE = Path(__file__).parent / "schema.sql"


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
        if current < CURRENT_VERSION:
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (CURRENT_VERSION, datetime.now(UTC).isoformat()),
            )
        conn.commit()
    finally:
        conn.close()
