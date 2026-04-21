"""Trigger manifest schema.

One ``manifest.yaml`` per trigger folder under ``triggers/``. At boot
the registry loads every manifest, validates it, and upserts a row in
the ``triggers`` table so the scheduler can pick it up.

Two schedule shapes — cron and event — discriminated by a ``kind``
field in the schedule mapping. That stays explicit rather than trying
to sniff the intent from a bare string, which would fall apart the
first time an event type happens to contain a digit.

Minimal manifest::

    name: morning_inbox_triage
    agent_name: inbox_triage_agent
    goal: "Triage the inbox for anything actionable in the last 24 hours."
    enabled: true
    schedule:
      kind: cron
      expression: "0 7 * * *"

    # ... or an event-driven one:
    schedule:
      kind: event
      event_type: sentinel.incident
      filter:
        severity: HIGH

Notes:

- Manifests are read-only at runtime. Enable/disable toggles live in
  the SQLite ``triggers`` table so they survive restarts without a
  YAML edit; ``enabled`` in the manifest is just the initial seed.
- Event filter is a flat dict of exact-match fields on the event
  payload. Dotted paths and comparison operators can land later
  without breaking the wire format.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from core.triggers.cron import CronParseError, parse_cron

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class CronScheduleSpec(BaseModel):
    kind: Literal["cron"] = "cron"
    expression: str = Field(
        ...,
        description="5-field cron expression — minute hour dom month dow.",
    )

    @field_validator("expression")
    @classmethod
    def _parseable(cls, v: str) -> str:
        try:
            parse_cron(v)
        except CronParseError as e:
            raise ValueError(str(e)) from e
        return v.strip()


class EventScheduleSpec(BaseModel):
    kind: Literal["event"] = "event"
    event_type: str = Field(
        ...,
        description=(
            "Hub event type to listen for (e.g. 'sentinel.incident'). "
            "Matched exactly; no wildcarding."
        ),
    )
    filter: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "Optional exact-match filter. Each key must be present in "
            "the event payload and equal the provided value. Empty "
            "dict → fire on every event of this type."
        ),
    )


ScheduleSpec = CronScheduleSpec | EventScheduleSpec


class TriggerManifest(BaseModel):
    name: str = Field(..., description="Slug; matches the folder name.")
    description: str = ""
    agent_name: str = Field(
        ...,
        description=(
            "Registered agent to run when the trigger fires. Must "
            "already be installed via agents/."
        ),
    )
    goal: str = Field(
        ...,
        description=(
            "Static prompt passed to ``orchestrator.agent_run``. No "
            "template interpolation in V1 — write the full sentence."
        ),
    )
    enabled: bool = Field(
        default=True,
        description=(
            "Initial enabled-state when the manifest is first seen. "
            "The operator can flip this at runtime via the UI; the "
            "choice persists in the triggers table and overrides the "
            "manifest on subsequent boots."
        ),
    )
    schedule: ScheduleSpec = Field(..., discriminator="kind")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_PATTERN.match(v):
            raise ValueError(f"invalid trigger name: {v!r}")
        return v

    @field_validator("agent_name")
    @classmethod
    def _agent_name(cls, v: str) -> str:
        if not NAME_PATTERN.match(v):
            raise ValueError(f"invalid agent_name reference: {v!r}")
        return v

    @field_validator("goal")
    @classmethod
    def _goal(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("goal must be a non-empty string")
        return cleaned

    @classmethod
    def load(cls, path: Path) -> TriggerManifest:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"trigger manifest {path} is not a mapping")
        return cls.model_validate(raw)


__all__ = [
    "CronScheduleSpec",
    "EventScheduleSpec",
    "ScheduleSpec",
    "TriggerManifest",
]
