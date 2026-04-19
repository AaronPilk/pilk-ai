"""Data shapes shared across every Sentinel module.

Kept import-cycle-free — nothing in this module depends on anything
else in ``core.sentinel``. Stores, rules, triage, and supervisor all
reach back here for the types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    """How urgently an incident needs human attention.

    Order is meaningful: comparison operators on plain strings give the
    wrong answer, so compare via ``rank()`` when thresholding.
    """

    LOW = "low"
    MED = "med"
    HIGH = "high"
    CRITICAL = "critical"

    @classmethod
    def parse(cls, raw: str) -> Severity:
        """Tolerant parser for LLM-returned triage payloads. Unknown
        values map to ``MED`` — fail-safe (operator sees it, doesn't
        flood notifications)."""
        try:
            return cls(raw.strip().lower())
        except (ValueError, AttributeError):
            return cls.MED

    def rank(self) -> int:
        return {
            Severity.LOW: 0,
            Severity.MED: 1,
            Severity.HIGH: 2,
            Severity.CRITICAL: 3,
        }[self]


class Category(StrEnum):
    """Coarse failure taxonomy. Each category maps to at most one
    remediation in ``remediate.ALLOWED_REMEDIATIONS``; categories not
    in the remediation map escalate directly to notify."""

    STALE_HEARTBEAT = "stale_heartbeat"
    ERROR_BURST = "error_burst"
    CRASH_SIGNATURE = "crash_signature"
    SCHEMA_VIOLATION = "schema_violation"
    STUCK_TASK = "stuck_task"
    DUPLICATE_WORK = "duplicate_work"
    LOCK_FILE_ORPHAN = "lock_file_orphan"
    RATE_LIMITED = "rate_limited"
    TRANSIENT_API_ERROR = "transient_api_error"
    DISK_FULL = "disk_full"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: str) -> Category:
        try:
            return cls(raw.strip().lower())
        except (ValueError, AttributeError):
            return cls.UNKNOWN


@dataclass(frozen=True)
class Heartbeat:
    """One row from ``agent_heartbeats``."""

    agent_name: str
    status: str  # "ok" | "degraded" | "disabled"
    progress: str | None
    active_task_id: str | None
    last_at: str
    interval_seconds: int
    stuck_task_timeout_seconds: int


@dataclass(frozen=True)
class Finding:
    """What a rule returns when it fires.

    Keep the shape narrow: the rule's job is to describe *what was
    observed*, not what to do about it. Triage (Tier 1) translates a
    Finding into ``(severity, category, recommended_action)``.
    """

    kind: str  # the rule id that fired, e.g. "stale_heartbeat"
    agent_name: str | None
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    # A stable hash of the underlying observation. Two findings with
    # equal dedupe_keys coalesce within the supervisor's dedupe window.
    dedupe_key: str = ""


@dataclass(frozen=True)
class TriageResult:
    """Tier-1 output. ``confidence`` is a soft hint at how reliable the
    triage LLM call was — below 0.5 the supervisor prefers notify over
    remediate."""

    severity: Severity
    category: Category
    likely_cause: str
    recommended_action: str
    confidence: float = 1.0


@dataclass(frozen=True)
class Incident:
    """One row from ``sentinel_incidents`` — the persistent record of
    everything Sentinel has observed."""

    id: str
    agent_name: str | None
    category: Category
    severity: Severity
    finding_kind: str
    summary: str
    details: dict[str, Any]
    triage: TriageResult | None
    remediation: str | None  # "restarted" | "retried" | None
    outcome: str | None  # "ok" | "failed: <reason>" | None
    acknowledged_at: str | None
    created_at: str


__all__ = [
    "Category",
    "Finding",
    "Heartbeat",
    "Incident",
    "Severity",
    "TriageResult",
]
