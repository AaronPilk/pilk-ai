"""Tests for the trigger manifest schema.

Covers the YAML loader + the Pydantic validation boundaries —
name pattern, cron parse-through, schedule discriminator, goal
non-emptiness.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from pydantic import ValidationError

from core.triggers.manifest import (
    CronScheduleSpec,
    EventScheduleSpec,
    TriggerManifest,
)


def _write(path: Path, body: str) -> Path:
    path.write_text(dedent(body), encoding="utf-8")
    return path


# ── happy path ──────────────────────────────────────────────────


def test_load_cron_manifest(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "m.yaml",
        """
        name: morning_digest
        description: Morning inbox digest.
        agent_name: inbox_triage_agent
        goal: "Triage the inbox for anything actionable."
        enabled: true
        schedule:
          kind: cron
          expression: "0 7 * * *"
        """,
    )
    m = TriggerManifest.load(p)
    assert m.name == "morning_digest"
    assert isinstance(m.schedule, CronScheduleSpec)
    assert m.schedule.expression == "0 7 * * *"
    assert m.enabled is True


def test_load_event_manifest(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "m.yaml",
        """
        name: high_sev_paging
        agent_name: inbox_triage_agent
        goal: "Page the operator about this incident."
        schedule:
          kind: event
          event_type: sentinel.incident
          filter:
            severity: HIGH
        """,
    )
    m = TriggerManifest.load(p)
    assert isinstance(m.schedule, EventScheduleSpec)
    assert m.schedule.event_type == "sentinel.incident"
    assert m.schedule.filter == {"severity": "HIGH"}


# ── validation errors ───────────────────────────────────────────


def test_invalid_name_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "m.yaml",
        """
        name: Bad-Name
        agent_name: x
        goal: hi
        schedule: { kind: cron, expression: "0 0 * * *" }
        """,
    )
    with pytest.raises(ValidationError):
        TriggerManifest.load(p)


def test_invalid_cron_rejected_at_parse(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "m.yaml",
        """
        name: bad_cron
        agent_name: x
        goal: hi
        schedule: { kind: cron, expression: "not a cron" }
        """,
    )
    with pytest.raises(ValidationError):
        TriggerManifest.load(p)


def test_empty_goal_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "m.yaml",
        """
        name: empty_goal
        agent_name: x
        goal: "   "
        schedule: { kind: cron, expression: "0 0 * * *" }
        """,
    )
    with pytest.raises(ValidationError):
        TriggerManifest.load(p)


def test_non_mapping_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path / "m.yaml", "- not a mapping\n- but a list\n")
    with pytest.raises(ValueError):
        TriggerManifest.load(p)
