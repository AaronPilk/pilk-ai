"""Sandbox manager.

Holds the live set of sandboxes, keyed by a stable id derived from agent
name + profile so an agent's state persists across runs. Currently only
process sandboxes are implemented; browser + remote drivers slot in by
adding another `type` branch in `get_or_create`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from core.db import connect
from core.logging import get_logger
from core.sandbox.base import Sandbox, SandboxDescription
from core.sandbox.process import ProcessSandbox

log = get_logger("pilkd.sandbox")


class SandboxManager:
    def __init__(self, sandboxes_dir: Path, db_path: Path) -> None:
        self.sandboxes_dir = sandboxes_dir
        self.db_path = db_path
        self._by_id: dict[str, Sandbox] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _sandbox_id(*, type: str, agent_name: str | None, profile: str) -> str:
        agent = agent_name or "shared"
        return f"sb_{type}_{agent}_{profile}"

    async def get_or_create(
        self,
        *,
        type: str,
        agent_name: str | None,
        profile: str,
        capabilities: frozenset[str] | None = None,
    ) -> Sandbox:
        sb_id = self._sandbox_id(type=type, agent_name=agent_name, profile=profile)
        caps = capabilities or frozenset()
        async with self._lock:
            existing = self._by_id.get(sb_id)
            if existing is not None and existing.description.state != "destroyed":
                # Capabilities travel with the manifest; a restart may widen or
                # narrow them. Refresh the live description rather than trust
                # stale state.
                existing.description.capabilities = caps
                return existing

            root = self.sandboxes_dir / sb_id
            if type == "process":
                sb: Sandbox = ProcessSandbox(
                    sandbox_id=sb_id,
                    agent_name=agent_name,
                    profile=profile,
                    root=root,
                    capabilities=caps,
                )
            else:
                raise NotImplementedError(
                    f"sandbox type {type!r} not available in this batch"
                )
            await sb.ensure()
            self._by_id[sb_id] = sb
            await self._upsert(sb.description)
            log.info(
                "sandbox_ready",
                sandbox_id=sb_id,
                type=type,
                agent=agent_name,
                profile=profile,
            )
            return sb

    async def destroy(self, sandbox_id: str) -> None:
        async with self._lock:
            sb = self._by_id.pop(sandbox_id, None)
        if sb is None:
            return
        await sb.destroy()
        await self._mark_destroyed(sandbox_id)

    def list_in_memory(self) -> list[SandboxDescription]:
        return [s.description for s in self._by_id.values()]

    async def list_all(self) -> list[dict]:
        async with connect(self.db_path) as conn, conn.execute(
            "SELECT id, type, agent_name, state, created_at, destroyed_at, "
            "metadata_json FROM sandboxes ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            live = self._by_id.get(d["id"])
            if live:
                d["workspace"] = str(live.description.workspace)
                d["profile"] = live.description.profile
                d["state"] = live.description.state
                d["capabilities"] = sorted(live.description.capabilities)
            else:
                d["capabilities"] = []
            out.append(d)
        return out

    async def _upsert(self, desc: SandboxDescription) -> None:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT id FROM sandboxes WHERE id = ?", (desc.id,)
            ) as cur:
                existing = await cur.fetchone()
            if existing is None:
                await conn.execute(
                    "INSERT INTO sandboxes(id, type, agent_name, state, "
                    "created_at) VALUES (?, ?, ?, ?, ?)",
                    (desc.id, desc.type, desc.agent_name, desc.state, desc.created_at),
                )
            else:
                await conn.execute(
                    "UPDATE sandboxes SET state = ?, destroyed_at = NULL WHERE id = ?",
                    (desc.state, desc.id),
                )
            await conn.commit()

    async def _mark_destroyed(self, sandbox_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        async with connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE sandboxes SET state = 'destroyed', destroyed_at = ? "
                "WHERE id = ?",
                (now, sandbox_id),
            )
            await conn.commit()
