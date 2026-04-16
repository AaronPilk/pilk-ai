"""Temporary trust rules.

A trust rule says: for the next N seconds, calls that match (agent, tool,
and optionally a subset of args) may skip approval. Rules are in-memory —
they're meant for a single work session, not persisted across daemon
restarts. That tightness is intentional: a long-lived whitelist is the
opposite of what this layer is for.

Rules are always scoped; a wildcard agent (`None`) applies to the free
chat path and every agent alike. The `args_matcher` uses subset semantics:
every key/value it names must be present and equal in the incoming args;
unmentioned keys are ignored. That lets the user approve "any fetch of
docs.python.org" without locking in a fragile exact-match.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TrustRule:
    id: str
    agent_name: str | None
    tool_name: str
    args_matcher: dict[str, Any]
    expires_at: float   # unix epoch seconds, monotonic-style via time.time()
    created_at: float
    created_by: str = "user"
    reason: str | None = None
    uses: int = 0

    def matches(
        self, *, agent_name: str | None, tool_name: str, args: dict[str, Any]
    ) -> bool:
        if self.tool_name != tool_name:
            return False
        if self.agent_name is not None and self.agent_name != agent_name:
            return False
        return all(args.get(k) == v for k, v in self.args_matcher.items())

    def is_expired(self, *, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "tool_name": self.tool_name,
            "args_matcher": dict(self.args_matcher),
            "expires_at": self.expires_at,
            "expires_in_s": max(0, int(self.expires_at - time.time())),
            "created_at": self.created_at,
            "created_by": self.created_by,
            "reason": self.reason,
            "uses": self.uses,
        }


@dataclass
class TrustStore:
    """Small thread-safe store for live trust rules.

    No persistence: rules evaporate on restart, and that's the point.
    """

    _rules: list[TrustRule] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(
        self,
        *,
        agent_name: str | None,
        tool_name: str,
        args_matcher: dict[str, Any] | None = None,
        ttl_seconds: int,
        reason: str | None = None,
        created_by: str = "user",
    ) -> TrustRule:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        now = time.time()
        rule = TrustRule(
            id=f"trust_{uuid.uuid4().hex[:10]}",
            agent_name=agent_name,
            tool_name=tool_name,
            args_matcher=dict(args_matcher or {}),
            expires_at=now + ttl_seconds,
            created_at=now,
            created_by=created_by,
            reason=reason,
        )
        with self._lock:
            self._rules.append(rule)
        return rule

    def match(
        self, *, agent_name: str | None, tool_name: str, args: dict[str, Any]
    ) -> TrustRule | None:
        self._purge_expired()
        with self._lock:
            for rule in self._rules:
                if rule.matches(
                    agent_name=agent_name, tool_name=tool_name, args=args
                ):
                    rule.uses += 1
                    return rule
        return None

    def revoke(self, rule_id: str) -> bool:
        with self._lock:
            before = len(self._rules)
            self._rules = [r for r in self._rules if r.id != rule_id]
            return len(self._rules) < before

    def list(self) -> list[TrustRule]:
        self._purge_expired()
        with self._lock:
            return list(self._rules)

    def _purge_expired(self) -> None:
        now = time.time()
        with self._lock:
            self._rules = [r for r in self._rules if not r.is_expired(now=now)]
