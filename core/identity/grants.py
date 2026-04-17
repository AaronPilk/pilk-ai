"""Runtime grants — which agents may use which connected accounts.

One JSON file at `~/PILK/identity/grants.json`. The schema is small:

    {
      "agents": {
        "outreach_agent": { "accounts": ["google-user-aaron-work-com"] },
        "triage_agent":   { "accounts": [] }
      }
    }

Rules:

- An agent **absent** from `agents` is permissive (back-compat). Existing
  agents installed before Batch N therefore keep working.
- An agent **present** but with an empty `accounts` list is restrictive:
  the Gateway rejects any tool call from that agent that needs a
  connected account.
- Top-level chat (no agent) bypasses this layer — you implicitly trust
  yourself. Only agent calls are gated here.

Writes are atomic; a single in-process lock serializes mutations.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from core.logging import get_logger

log = get_logger("pilkd.identity.grants")


@dataclass
class AgentGrant:
    agent_name: str
    accounts: list[str] = field(default_factory=list)
    granted_at: str | None = None
    granted_by: str = "user"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class GrantsStore:
    def __init__(self, home: Path) -> None:
        self._path = home / "identity" / "grants.json"
        self._lock = Lock()

    # ── reads ─────────────────────────────────────────────────────

    def has_entry(self, agent_name: str) -> bool:
        """Is this agent explicitly tracked (permissive agents return False)."""
        return agent_name in self._read().get("agents", {})

    def accounts_for(self, agent_name: str) -> list[str]:
        """Granted account_ids. Empty list means restrictive-empty."""
        entry = self._read().get("agents", {}).get(agent_name)
        if entry is None:
            return []
        return list(entry.get("accounts") or [])

    def agents_for(self, account_id: str) -> list[str]:
        out: list[str] = []
        for name, entry in self._read().get("agents", {}).items():
            if account_id in (entry.get("accounts") or []):
                out.append(name)
        return sorted(out)

    def all(self) -> dict[str, AgentGrant]:
        data = self._read().get("agents", {})
        return {
            name: AgentGrant(
                agent_name=name,
                accounts=list(entry.get("accounts") or []),
                granted_at=entry.get("granted_at"),
                granted_by=entry.get("granted_by", "user"),
            )
            for name, entry in data.items()
        }

    def allows(self, agent_name: str, account_id: str) -> bool:
        """Final yes/no. Permissive fallback handled here.

        - Unknown agent (no entry) → True (back-compat).
        - Known agent with account_id in list → True.
        - Known agent otherwise → False.
        """
        entry = self._read().get("agents", {}).get(agent_name)
        if entry is None:
            return True
        return account_id in (entry.get("accounts") or [])

    # ── writes ────────────────────────────────────────────────────

    def register_agent(
        self,
        agent_name: str,
        *,
        accounts: list[str] | None = None,
        granted_by: str = "user",
    ) -> None:
        """Create an explicit entry. Used by agent_create to force
        opt-in semantics on every newly-built agent.

        Idempotent: if an entry already exists, it's left alone.
        """
        with self._lock:
            data = self._read_locked()
            agents = data.setdefault("agents", {})
            if agent_name in agents:
                return
            agents[agent_name] = {
                "accounts": list(accounts or []),
                "granted_at": _now(),
                "granted_by": granted_by,
            }
            self._write_locked(data)
        log.info(
            "agent_grant_registered",
            agent=agent_name,
            accounts=list(accounts or []),
        )

    def grant(self, agent_name: str, account_id: str) -> bool:
        with self._lock:
            data = self._read_locked()
            agents = data.setdefault("agents", {})
            entry = agents.setdefault(
                agent_name,
                {"accounts": [], "granted_at": _now(), "granted_by": "user"},
            )
            accounts = list(entry.get("accounts") or [])
            if account_id in accounts:
                return False
            accounts.append(account_id)
            entry["accounts"] = accounts
            entry["granted_at"] = _now()
            self._write_locked(data)
        log.info("agent_grant_added", agent=agent_name, account_id=account_id)
        return True

    def revoke(self, agent_name: str, account_id: str) -> bool:
        with self._lock:
            data = self._read_locked()
            entry = data.get("agents", {}).get(agent_name)
            if entry is None:
                return False
            before = entry.get("accounts") or []
            after = [a for a in before if a != account_id]
            if len(after) == len(before):
                return False
            entry["accounts"] = after
            entry["granted_at"] = _now()
            self._write_locked(data)
        log.info("agent_grant_removed", agent=agent_name, account_id=account_id)
        return True

    def remove_agent(self, agent_name: str) -> None:
        """Drop an agent's entry entirely — returns it to permissive."""
        with self._lock:
            data = self._read_locked()
            if data.get("agents", {}).pop(agent_name, None) is None:
                return
            self._write_locked(data)
        log.info("agent_grant_cleared", agent=agent_name)

    def remove_account_everywhere(self, account_id: str) -> int:
        """When an account is removed from the store, drop it from every
        grant list. Returns the number of agents affected.
        """
        changed = 0
        with self._lock:
            data = self._read_locked()
            for entry in data.get("agents", {}).values():
                before = entry.get("accounts") or []
                after = [a for a in before if a != account_id]
                if len(after) != len(before):
                    entry["accounts"] = after
                    changed += 1
            if changed:
                self._write_locked(data)
        return changed

    # ── internals ─────────────────────────────────────────────────

    def _read(self) -> dict:
        with self._lock:
            return self._read_locked()

    def _read_locked(self) -> dict:
        if not self._path.exists():
            return {"agents": {}}
        try:
            return json.loads(self._path.read_text())
        except Exception as e:
            log.warning("grants_unreadable", error=str(e))
            return {"agents": {}}

    def _write_locked(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, self._path)
        with contextlib.suppress(Exception):
            self._path.chmod(0o600)


# Convenience export shape for callers that want a plain dict.
def as_public(grant: AgentGrant) -> dict:
    return asdict(grant)
