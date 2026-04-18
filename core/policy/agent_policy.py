"""Per-agent autonomy profile store.

The gate consults this store at every tool call to find the agent's
autonomy profile, which widens its auto-allow set. Designed to be
read-hot and write-rare, so the store caches values in memory and
syncs to SQLite on set. Unknown agents default to `assistant`.

Valid profile strings live in `core.policy.gate.PROFILE_AUTO_ALLOW`;
this store does not validate — the gate does, falling back to the
default if the stored value is unknown.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path

from core.db import connect

DEFAULT_PROFILE = "assistant"
VALID_PROFILES: frozenset[str] = frozenset(
    {"observer", "assistant", "operator", "autonomous"}
)


class AgentPolicyStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._cache: dict[str, str] = {}
        self._loaded = False
        self._lock = threading.Lock()
        self._load_lock = asyncio.Lock()

    async def hydrate(self) -> None:
        """Populate the in-memory cache from SQLite. Safe to call twice."""
        async with self._load_lock:
            if self._loaded:
                return
            async with connect(self.db_path) as conn, conn.execute(
                "SELECT agent_name, profile FROM agent_policies"
            ) as cur:
                rows = await cur.fetchall()
            with self._lock:
                for r in rows:
                    self._cache[r["agent_name"]] = r["profile"]
                self._loaded = True

    def get(self, agent_name: str | None) -> str:
        """Sync lookup used by the gate. Returns the default for None or
        unknown agents. This is a hot path — no IO."""
        if agent_name is None:
            return DEFAULT_PROFILE
        with self._lock:
            return self._cache.get(agent_name, DEFAULT_PROFILE)

    async def set(self, agent_name: str, profile: str) -> str:
        """Persist and cache `profile` for `agent_name`. Raises
        ValueError for unknown profiles — the UI should only send known
        strings."""
        if profile not in VALID_PROFILES:
            raise ValueError(
                f"unknown profile {profile!r}; expected one of {sorted(VALID_PROFILES)}"
            )
        now = datetime.now(UTC).isoformat()
        async with connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO agent_policies(agent_name, profile, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(agent_name) DO UPDATE SET "
                "profile = excluded.profile, updated_at = excluded.updated_at",
                (agent_name, profile, now),
            )
            await conn.commit()
        with self._lock:
            self._cache[agent_name] = profile
        return profile

    def all(self) -> dict[str, str]:
        with self._lock:
            return dict(self._cache)


__all__ = ["DEFAULT_PROFILE", "VALID_PROFILES", "AgentPolicyStore"]
