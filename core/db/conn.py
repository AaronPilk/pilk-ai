"""Async SQLite connection helper.

PILK runs one daemon process with one SQLite file in WAL mode. Short-lived
connections are fine — concurrent readers are supported and writes are
serialized by SQLite itself. Every helper here opens, does the work, and
closes. No pool, no singletons — one less thing to get wrong.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite


@asynccontextmanager
async def connect(db_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = aiosqlite.Row
        yield conn
    finally:
        await conn.close()
