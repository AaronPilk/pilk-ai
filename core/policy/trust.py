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

Permanent rules (PR E) are the narrow exception to "no long-lived
whitelist." They're seeded at daemon startup for tool+argument shapes
the operator has pre-audited (e.g. ``agent_email_deliver`` to a small
set of internal addresses) and never expire. They still purge on restart
— we don't persist them — so a full reset reverts to "everything needs
approval." That's the invariant the approval layer relies on.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ArgsPredicate = Callable[[dict[str, Any]], bool]
"""Optional predicate run in addition to ``args_matcher``. Useful when
the allowed-args shape is a set-membership check (e.g. "every recipient
in ``to`` must be in this allowlist") rather than scalar equality."""


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
    # Permanent rules live until the daemon restarts. They ignore
    # ``expires_at`` entirely; we still keep it populated (as +inf) so
    # the serializer doesn't special-case it.
    permanent: bool = False
    # Per-call custom predicate. Lives only in-memory (callables don't
    # persist), so ``predicate`` rules must be seeded by code at startup.
    predicate: ArgsPredicate | None = None
    # Human-readable label for the matcher so `list()` surfaces
    # *why* a permanent rule exists without revealing the predicate
    # closure. Optional — falls back to an empty string.
    predicate_label: str = ""

    def matches(
        self, *, agent_name: str | None, tool_name: str, args: dict[str, Any]
    ) -> bool:
        if self.tool_name != tool_name:
            return False
        if self.agent_name is not None and self.agent_name != agent_name:
            return False
        if not all(args.get(k) == v for k, v in self.args_matcher.items()):
            return False
        return not (self.predicate is not None and not self.predicate(args))

    def is_expired(self, *, now: float | None = None) -> bool:
        if self.permanent:
            return False
        return (now or time.time()) >= self.expires_at

    def public_dict(self) -> dict[str, Any]:
        expires_in: int | None = (
            None
            if self.permanent
            else max(0, int(self.expires_at - time.time()))
        )
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "tool_name": self.tool_name,
            "args_matcher": dict(self.args_matcher),
            "expires_at": None if self.permanent else self.expires_at,
            "expires_in_s": expires_in,
            "permanent": self.permanent,
            "predicate_label": self.predicate_label or None,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "reason": self.reason,
            "uses": self.uses,
        }


@dataclass
class TrustStore:
    """Small thread-safe store for live trust rules.

    No persistence: rules evaporate on restart, and that's the point.
    Permanent rules also don't persist — they're re-seeded from code
    on every daemon boot so a compromised DB can't forge trust.
    """

    _rules: list[TrustRule] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(
        self,
        *,
        agent_name: str | None,
        tool_name: str,
        args_matcher: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
        reason: str | None = None,
        created_by: str = "user",
        permanent: bool = False,
        predicate: ArgsPredicate | None = None,
        predicate_label: str = "",
    ) -> TrustRule:
        """Register a new rule.

        Either ``ttl_seconds`` (positive int) OR ``permanent=True`` must
        be supplied. The two are mutually exclusive: permanent rules
        override TTL, and callers must be explicit about which shape
        they want.
        """
        if permanent and ttl_seconds is not None:
            raise ValueError(
                "permanent rules must not specify ttl_seconds — they never expire"
            )
        if not permanent and (ttl_seconds is None or ttl_seconds <= 0):
            raise ValueError("ttl_seconds must be > 0 (or use permanent=True)")
        now = time.time()
        expires_at = float("inf") if permanent else now + int(ttl_seconds)
        rule = TrustRule(
            id=f"trust_{uuid.uuid4().hex[:10]}",
            agent_name=agent_name,
            tool_name=tool_name,
            args_matcher=dict(args_matcher or {}),
            expires_at=expires_at,
            created_at=now,
            created_by=created_by,
            reason=reason,
            permanent=permanent,
            predicate=predicate,
            predicate_label=predicate_label,
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
