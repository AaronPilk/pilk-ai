"""Sentinel — in-process supervisor for every other PILK agent.

Design principles (see ``agents/sentinel/CONTRACT.md``):

1. **Event-driven.** No polling loop runs an LLM call. Sentinel sleeps
   until a Hub event arrives OR the periodic heartbeat scan ticks.
2. **Tiered escalation.** Most work is Tier 0 (pure Python rules over
   structured data). Tier 1 (Haiku triage) runs only when a rule fires.
   Tier 2 (remediation) runs only on allowlisted categories.
3. **Bounded autonomy.** Remediation is a fixed dict of
   ``category → action``; anything else becomes a notification, not
   an action.
4. **Observability.** Every Sentinel decision is persisted to
   ``sentinel_incidents`` with the tier it operated at, the rule that
   fired, and the outcome (if any remediation ran).

Public surface:

    Supervisor      — orchestrator, wired in the FastAPI lifespan.
    HeartbeatStore  — persistent agent liveness tracking.
    IncidentStore   — persistent finding log (SQLite + jsonl mirror).
    Rule / Finding  — rule-engine protocol + output shape.
    Severity / Category — enums shared by triage + remediation.
"""

from core.sentinel.contracts import (
    Category,
    Finding,
    Heartbeat,
    Incident,
    Severity,
    TriageResult,
)
from core.sentinel.heartbeats import HeartbeatStore
from core.sentinel.incidents import IncidentStore
from core.sentinel.rules import (
    BUILTIN_RULES,
    Rule,
    RuleContext,
    run_rules,
)
from core.sentinel.supervisor import Supervisor

__all__ = [
    "BUILTIN_RULES",
    "Category",
    "Finding",
    "Heartbeat",
    "HeartbeatStore",
    "Incident",
    "IncidentStore",
    "Rule",
    "RuleContext",
    "Severity",
    "Supervisor",
    "TriageResult",
    "run_rules",
]
