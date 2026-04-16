"""Agent registry.

On startup the registry walks the repo's `/agents/` directory, loads every
`manifest.yaml` it finds, validates it, and upserts a row in the `agents`
table. After that the registry is queried by name (orchestrator) or listed
(dashboard). The in-memory manifests map is the source of truth while the
daemon runs; the DB mirror is there so cross-session history (last run,
state) persists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from core.db import connect
from core.logging import get_logger
from core.registry.manifest import Manifest

log = get_logger("pilkd.registry")


class AgentNotFoundError(LookupError):
    pass


class AgentRegistry:
    def __init__(self, manifests_dir: Path, db_path: Path) -> None:
        self.manifests_dir = manifests_dir
        self.db_path = db_path
        self._manifests: dict[str, Manifest] = {}

    def manifests(self) -> dict[str, Manifest]:
        return dict(self._manifests)

    def get(self, name: str) -> Manifest:
        m = self._manifests.get(name)
        if m is None:
            raise AgentNotFoundError(f"agent not registered: {name}")
        return m

    async def discover_and_install(self) -> list[str]:
        """Scan the manifests directory, validate, and upsert into the DB.

        Skips folders without a manifest.yaml and folders whose name starts
        with an underscore (reserved for templates). Returns the names of
        the agents now registered.
        """
        installed: list[str] = []
        if not self.manifests_dir.exists():
            return installed

        for sub in sorted(self.manifests_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith("_"):
                continue
            manifest_path = sub / "manifest.yaml"
            if not manifest_path.exists():
                continue
            try:
                manifest = Manifest.load(manifest_path)
            except (OSError, ValueError) as exc:
                log.warning(
                    "manifest_invalid",
                    path=str(manifest_path),
                    error=str(exc),
                )
                continue
            if manifest.name != sub.name:
                log.warning(
                    "manifest_name_mismatch",
                    folder=sub.name,
                    manifest_name=manifest.name,
                )
                continue
            self._manifests[manifest.name] = manifest
            await self._upsert(manifest, manifest_path)
            installed.append(manifest.name)
            log.info(
                "agent_registered",
                name=manifest.name,
                version=manifest.version,
                tools=manifest.tools,
                sandbox=manifest.sandbox.type,
            )
        return installed

    async def _upsert(self, manifest: Manifest, manifest_path: Path) -> None:
        now = datetime.now(UTC).isoformat()
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT name FROM agents WHERE name = ?", (manifest.name,)
            ) as cur:
                existing = await cur.fetchone()
            if existing is None:
                await conn.execute(
                    "INSERT INTO agents(name, version, manifest_path, state, "
                    "installed_at) VALUES (?, ?, ?, 'ready', ?)",
                    (manifest.name, manifest.version, str(manifest_path), now),
                )
            else:
                await conn.execute(
                    "UPDATE agents SET version = ?, manifest_path = ?, "
                    "state = CASE WHEN state = 'errored' THEN 'ready' ELSE state END "
                    "WHERE name = ?",
                    (manifest.version, str(manifest_path), manifest.name),
                )
            await conn.commit()

    async def mark_state(self, name: str, state: str) -> None:
        now = datetime.now(UTC).isoformat()
        async with connect(self.db_path) as conn:
            if state == "running":
                await conn.execute(
                    "UPDATE agents SET state = ?, last_run_at = ? WHERE name = ?",
                    (state, now, name),
                )
            else:
                await conn.execute(
                    "UPDATE agents SET state = ? WHERE name = ?", (state, name)
                )
            await conn.commit()

    async def list_rows(self) -> list[dict]:
        async with connect(self.db_path) as conn, conn.execute(
            "SELECT name, version, manifest_path, state, installed_at, last_run_at "
            "FROM agents ORDER BY name ASC"
        ) as cur:
            rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            m = self._manifests.get(d["name"])
            if m:
                d["description"] = m.description
                d["tools"] = m.tools
                d["sandbox"] = m.sandbox.model_dump()
                d["budget"] = m.policy.budget.model_dump()
            out.append(d)
        return out
