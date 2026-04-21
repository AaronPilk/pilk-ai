"""Proactive triggers — turn PILK from reactive chat into a background OS.

A trigger is a YAML manifest that binds either a cron schedule or a hub
event filter to an agent + goal. When it fires, the scheduler calls
``orchestrator.agent_run(agent_name, goal)`` — identical to what happens
when the operator hits "Run" on the Agents tab, just invoked by the
clock or a signal instead of a human click.

Two kinds of schedule:

    cron   — "0 7 * * *"   → run at 07:00 UTC every day
    event  — hub event_type + optional filter dict (exact-match per key)

The registry loads manifests from ``triggers/`` at boot (same pattern as
``agents/``) and mirrors enabled-state + last_fired_at in SQLite so the
dashboard can show history across restarts.
"""

from __future__ import annotations

from core.triggers.cron import CronSchedule, parse_cron
from core.triggers.manifest import (
    CronScheduleSpec,
    EventScheduleSpec,
    TriggerManifest,
)
from core.triggers.registry import TriggerNotFoundError, TriggerRegistry
from core.triggers.scheduler import TriggerScheduler

__all__ = [
    "CronSchedule",
    "CronScheduleSpec",
    "EventScheduleSpec",
    "TriggerManifest",
    "TriggerNotFoundError",
    "TriggerRegistry",
    "TriggerScheduler",
    "parse_cron",
]
