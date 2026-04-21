"""Trigger registry.

Mirrors the agent registry pattern: walk ``triggers/`` at boot, load
each ``manifest.yaml``, validate, upsert into the ``triggers`` table.
Enabled-state + last_fired_at live in SQLite so the operator's toggle
survives restarts. The manifest itself is the source of truth for
structural data (schedule, agent_name, goal) — we re-read on every
boot and reconcile.

The scheduler consults the registry (not the DB directly) when it
needs to know which triggers are live + when they last fired.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from core.db import connect
from core.logging import get_logger
from core.triggers.manifest import TriggerManifest

log = get_logger("pilkd.triggers.registry")


class TriggerNotFoundError(LookupError):
    pass


class TriggerRegistry:
    def __init__(self, manifests_dir: Path, db_path: Path) -> None:
        self.manifests_dir = manifests_dir
        self.db_path = db_path
        self._manifests: dict[str, TriggerManifest] = {}
        # Per-trigger runtime state. Mirrors the DB row but lives in
        # memory so the scheduler doesn't hit SQLite on every tick.
        self._state: dict[str, dict[str, object]] = {}

    # ── Views ────────────────────────────────────────────────────────

    def manifests(self) -> dict[str, TriggerManifest]:
        return dict(self._manifests)

    def get(self, name: str) -> TriggerManifest:
        m = self._manifests.get(name)
        if m is None:
            raise TriggerNotFoundError(f"trigger not registered: {name}")
        return m

    def enabled(self, name: str) -> bool:
        return bool(self._state.get(name, {}).get("enabled", True))

    def iter_enabled(self) -> Iterable[TriggerManifest]:
        for name, manifest in self._manifests.items():
            if self.enabled(name):
                yield manifest

    def last_fired_at(self, name: str) -> str | None:
        val = self._state.get(name, {}).get("last_fired_at")
        return val if isinstance(val, str) else None

    # ── Boot ─────────────────────────────────────────────────────────

    async def discover_and_install(self) -> list[str]:
        """Scan ``triggers/`` and upsert each valid manifest.

        Reconciliation rules:

        - Folder underscore-prefix → skipped (reserved for templates).
        - Folder name must match manifest.name → mismatch skipped with
          a warning. Matches the agents/ rule for consistency.
        - Enabled-state in SQLite wins over manifest default when the
          row already exists. A fresh row seeds from the manifest.
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
                manifest = TriggerManifest.load(manifest_path)
            except (OSError, ValueError) as exc:
                log.warning(
                    "trigger_manifest_invalid",
                    path=str(manifest_path),
                    error=str(exc),
                )
                continue
            if manifest.name != sub.name:
                log.warning(
                    "trigger_manifest_name_mismatch",
                    folder=sub.name,
                    manifest_name=manifest.name,
                )
                continue
            self._manifests[manifest.name] = manifest
            state = await self._upsert(manifest, manifest_path)
            self._state[manifest.name] = state
            installed.append(manifest.name)
            log.info(
                "trigger_registered",
                name=manifest.name,
                agent=manifest.agent_name,
                kind=manifest.schedule.kind,
                enabled=state["enabled"],
            )
        return installed

    # ── Runtime state mutations ──────────────────────────────────────

    async def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in self._manifests:
            raise TriggerNotFoundError(f"trigger not registered: {name}")
        self._state.setdefault(name, {})["enabled"] = enabled
        now = datetime.now(UTC).isoformat()
        async with connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE triggers SET enabled = ?, updated_at = ? WHERE name = ?",
                (1 if enabled else 0, now, name),
            )
            await conn.commit()

    async def mark_fired(self, name: str, *, at: datetime | None = None) -> str:
        """Record a fire event + return the ISO timestamp we persisted.

        The scheduler uses the returned value as the broadcast payload
        so the UI can render "last fired 3 minutes ago" without a
        second read.
        """
        if name not in self._manifests:
            raise TriggerNotFoundError(f"trigger not registered: {name}")
        stamp = (at or datetime.now(UTC)).isoformat()
        self._state.setdefault(name, {})["last_fired_at"] = stamp
        async with connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE triggers SET last_fired_at = ?, updated_at = ? WHERE name = ?",
                (stamp, stamp, name),
            )
            await conn.commit()
        return stamp

    async def list_rows(self) -> list[dict]:
        """UI-shaped listing. Reads from memory for hot fields, joins
        the manifest so the dashboard doesn't need a second round-trip
        just to render the schedule expression."""
        out: list[dict] = []
        for name, manifest in sorted(self._manifests.items()):
            state = self._state.get(name, {})
            out.append(
                {
                    "name": name,
                    "description": manifest.description,
                    "agent_name": manifest.agent_name,
                    "goal": manifest.goal,
                    "schedule": manifest.schedule.model_dump(),
                    "enabled": bool(state.get("enabled", manifest.enabled)),
                    "last_fired_at": state.get("last_fired_at"),
                }
            )
        return out

    # ── Internals ────────────────────────────────────────────────────

    async def _upsert(
        self, manifest: TriggerManifest, manifest_path: Path,
    ) -> dict[str, object]:
        now = datetime.now(UTC).isoformat()
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT enabled, last_fired_at FROM triggers WHERE name = ?",
                (manifest.name,),
            ) as cur:
                existing = await cur.fetchone()
            if existing is None:
                await conn.execute(
                    "INSERT INTO triggers(name, manifest_path, enabled, "
                    "last_fired_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, NULL, ?, ?)",
                    (
                        manifest.name,
                        str(manifest_path),
                        1 if manifest.enabled else 0,
                        now,
                        now,
                    ),
                )
                await conn.commit()
                return {"enabled": manifest.enabled, "last_fired_at": None}
            # Existing row — keep operator-chosen enabled-state, just
            # refresh the manifest_path in case the YAML moved.
            await conn.execute(
                "UPDATE triggers SET manifest_path = ?, updated_at = ? WHERE name = ?",
                (str(manifest_path), now, manifest.name),
            )
            await conn.commit()
            enabled = bool(existing["enabled"])
            last_fired_at = existing["last_fired_at"]
            return {
                "enabled": enabled,
                "last_fired_at": last_fired_at,
            }


__all__ = ["TriggerNotFoundError", "TriggerRegistry"]
