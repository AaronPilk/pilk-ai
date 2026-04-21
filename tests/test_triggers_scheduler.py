"""Tests for the trigger scheduler.

Covers the four behaviours that matter:
- cron triggers fire when the current minute matches and call the
  agent_run hook
- event triggers fire on matching hub events + respect filter dicts
- disabled triggers never fire
- fires broadcast ``trigger.fired`` on the hub
- same-minute re-evaluation is deduped
- lock serializes concurrent fires
- trigger.* events do NOT cause recursion
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent

import pytest

from core.api.hub import Hub
from core.config import get_settings
from core.db import ensure_schema
from core.triggers import TriggerRegistry, TriggerScheduler
from core.triggers.scheduler import _filter_matches


def _manifest(dir_: Path, name: str, body: str) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "manifest.yaml").write_text(dedent(body), encoding="utf-8")


async def _build_registry(
    tmp_path: Path, *manifests: tuple[str, str],
) -> TriggerRegistry:
    settings = get_settings()
    ensure_schema(settings.db_path)
    for name, body in manifests:
        _manifest(tmp_path / name, name, body)
    reg = TriggerRegistry(manifests_dir=tmp_path, db_path=settings.db_path)
    await reg.discover_and_install()
    return reg


class _FakeAgentRun:
    """Captures (agent_name, task) on every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, agent_name: str, task: str) -> None:
        self.calls.append((agent_name, task))


def _bcast_capture(log: list[tuple[str, dict]]) -> Awaitable[None]:
    async def broadcast(event_type: str, payload: dict) -> None:
        log.append((event_type, payload))
    return broadcast  # type: ignore[return-value]


# ── filter helper ────────────────────────────────────────────────


def test_filter_empty_matches_everything() -> None:
    assert _filter_matches({}, {"severity": "HIGH"})
    assert _filter_matches({}, {})


def test_filter_exact_match() -> None:
    assert _filter_matches({"severity": "HIGH"}, {"severity": "HIGH"})
    assert not _filter_matches({"severity": "HIGH"}, {"severity": "LOW"})
    assert not _filter_matches({"severity": "HIGH"}, {})  # missing key


# ── cron tick fires agent_run ────────────────────────────────────


@pytest.mark.asyncio
async def test_cron_tick_fires_matching_trigger(tmp_path: Path) -> None:
    reg = await _build_registry(
        tmp_path,
        (
            "daily",
            """
            name: daily
            agent_name: inbox_triage_agent
            goal: "Triage inbox."
            schedule:
              kind: cron
              expression: "0 7 * * *"
            """,
        ),
    )
    fake_run = _FakeAgentRun()
    bcast: list[tuple[str, dict]] = []

    sched = TriggerScheduler(
        registry=reg,
        hub=Hub(),
        agent_run=fake_run,
        broadcast=_bcast_capture(bcast),
        now_fn=lambda: datetime(2026, 4, 21, 7, 0, tzinfo=UTC),
    )
    await sched._tick()

    assert fake_run.calls == [("inbox_triage_agent", "Triage inbox.")]
    assert any(ev == "trigger.fired" for ev, _ in bcast)


@pytest.mark.asyncio
async def test_cron_tick_skips_non_matching(tmp_path: Path) -> None:
    reg = await _build_registry(
        tmp_path,
        (
            "daily",
            """
            name: daily
            agent_name: inbox_triage_agent
            goal: "Triage inbox."
            schedule: { kind: cron, expression: "0 7 * * *" }
            """,
        ),
    )
    fake_run = _FakeAgentRun()
    bcast: list[tuple[str, dict]] = []
    sched = TriggerScheduler(
        registry=reg, hub=Hub(), agent_run=fake_run,
        broadcast=_bcast_capture(bcast),
        now_fn=lambda: datetime(2026, 4, 21, 8, 30, tzinfo=UTC),
    )
    await sched._tick()
    assert fake_run.calls == []


@pytest.mark.asyncio
async def test_disabled_trigger_never_fires(tmp_path: Path) -> None:
    reg = await _build_registry(
        tmp_path,
        (
            "daily",
            """
            name: daily
            agent_name: inbox_triage_agent
            goal: "Triage inbox."
            enabled: false
            schedule: { kind: cron, expression: "0 7 * * *" }
            """,
        ),
    )
    fake_run = _FakeAgentRun()
    bcast: list[tuple[str, dict]] = []
    sched = TriggerScheduler(
        registry=reg, hub=Hub(), agent_run=fake_run,
        broadcast=_bcast_capture(bcast),
        now_fn=lambda: datetime(2026, 4, 21, 7, 0, tzinfo=UTC),
    )
    await sched._tick()
    assert fake_run.calls == []


@pytest.mark.asyncio
async def test_tick_dedupes_within_same_minute(tmp_path: Path) -> None:
    """Two ticks in the same wall-clock minute must not double-fire."""
    reg = await _build_registry(
        tmp_path,
        (
            "daily",
            """
            name: daily
            agent_name: inbox_triage_agent
            goal: "Triage inbox."
            schedule: { kind: cron, expression: "* * * * *" }
            """,
        ),
    )
    fake_run = _FakeAgentRun()
    bcast: list[tuple[str, dict]] = []
    stamp = datetime(2026, 4, 21, 7, 0, 30, tzinfo=UTC)
    sched = TriggerScheduler(
        registry=reg, hub=Hub(), agent_run=fake_run,
        broadcast=_bcast_capture(bcast),
        now_fn=lambda: stamp,
    )
    await sched._tick()
    await sched._tick()  # same minute, should no-op
    assert len(fake_run.calls) == 1


# ── event triggers ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_trigger_fires_on_matching_event(tmp_path: Path) -> None:
    reg = await _build_registry(
        tmp_path,
        (
            "page_high",
            """
            name: page_high
            agent_name: inbox_triage_agent
            goal: "Page the operator."
            schedule:
              kind: event
              event_type: sentinel.incident
              filter:
                severity: HIGH
            """,
        ),
    )
    fake_run = _FakeAgentRun()
    bcast: list[tuple[str, dict]] = []
    hub = Hub()
    sched = TriggerScheduler(
        registry=reg, hub=hub, agent_run=fake_run,
        broadcast=_bcast_capture(bcast),
    )
    await sched.start()
    try:
        # Non-matching filter — should not fire.
        await hub.broadcast("sentinel.incident", {"severity": "LOW"})
        # Matching.
        await hub.broadcast("sentinel.incident", {"severity": "HIGH"})
        # Give the create_task a tick to run.
        await asyncio.sleep(0.05)
    finally:
        await sched.stop()

    assert fake_run.calls == [("inbox_triage_agent", "Page the operator.")]


@pytest.mark.asyncio
async def test_event_trigger_ignores_non_matching_type(tmp_path: Path) -> None:
    reg = await _build_registry(
        tmp_path,
        (
            "page_high",
            """
            name: page_high
            agent_name: inbox_triage_agent
            goal: g
            schedule: { kind: event, event_type: sentinel.incident }
            """,
        ),
    )
    fake_run = _FakeAgentRun()
    bcast: list[tuple[str, dict]] = []
    hub = Hub()
    sched = TriggerScheduler(
        registry=reg, hub=hub, agent_run=fake_run,
        broadcast=_bcast_capture(bcast),
    )
    await sched.start()
    try:
        await hub.broadcast("plan.created", {"severity": "HIGH"})
        await asyncio.sleep(0.05)
    finally:
        await sched.stop()
    assert fake_run.calls == []


@pytest.mark.asyncio
async def test_trigger_events_never_recurse(tmp_path: Path) -> None:
    """A trigger cannot be configured to fire on its own trigger.fired
    event — the scheduler drops every ``trigger.*`` event at the
    on-event entry point."""
    reg = await _build_registry(
        tmp_path,
        (
            "recursive",
            """
            name: recursive
            agent_name: inbox_triage_agent
            goal: g
            schedule: { kind: event, event_type: trigger.fired }
            """,
        ),
    )
    fake_run = _FakeAgentRun()
    bcast: list[tuple[str, dict]] = []
    hub = Hub()
    sched = TriggerScheduler(
        registry=reg, hub=hub, agent_run=fake_run,
        broadcast=_bcast_capture(bcast),
    )
    await sched.start()
    try:
        await hub.broadcast("trigger.fired", {"name": "recursive"})
        await asyncio.sleep(0.05)
    finally:
        await sched.stop()
    assert fake_run.calls == []


# ── fire_now / manual one-shot ──────────────────────────────────


@pytest.mark.asyncio
async def test_fire_now_bypasses_schedule(tmp_path: Path) -> None:
    reg = await _build_registry(
        tmp_path,
        (
            "daily",
            """
            name: daily
            agent_name: inbox_triage_agent
            goal: manual-only
            schedule: { kind: cron, expression: "0 7 * * *" }
            """,
        ),
    )
    fake_run = _FakeAgentRun()
    bcast: list[tuple[str, dict]] = []
    sched = TriggerScheduler(
        registry=reg, hub=Hub(), agent_run=fake_run,
        broadcast=_bcast_capture(bcast),
        now_fn=lambda: datetime(2026, 4, 21, 12, 0, tzinfo=UTC),
    )
    summary = await sched.fire_now("daily")
    assert summary["status"] == "fired"
    assert fake_run.calls == [("inbox_triage_agent", "manual-only")]


# ── failure + skip paths ────────────────────────────────────────


@pytest.mark.asyncio
async def test_failing_agent_run_is_caught_and_broadcasts(
    tmp_path: Path,
) -> None:
    reg = await _build_registry(
        tmp_path,
        (
            "daily",
            """
            name: daily
            agent_name: inbox_triage_agent
            goal: g
            schedule: { kind: cron, expression: "* * * * *" }
            """,
        ),
    )

    async def bad_run(name: str, task: str) -> None:
        raise RuntimeError("agent blew up")

    bcast: list[tuple[str, dict]] = []
    sched = TriggerScheduler(
        registry=reg, hub=Hub(), agent_run=bad_run,
        broadcast=_bcast_capture(bcast),
        now_fn=lambda: datetime(2026, 4, 21, 7, 0, tzinfo=UTC),
    )
    summary = await sched.fire_now("daily")
    assert summary["status"] == "failed"
    assert any(ev == "trigger.failed" for ev, _ in bcast)


@pytest.mark.asyncio
async def test_concurrent_fires_serialized_via_lock(tmp_path: Path) -> None:
    """A second fire attempt while the first is still in flight must
    be skipped rather than queued — keeps the operator from coming
    back to a backlog of stale runs."""
    reg = await _build_registry(
        tmp_path,
        (
            "daily",
            """
            name: daily
            agent_name: inbox_triage_agent
            goal: g
            schedule: { kind: cron, expression: "* * * * *" }
            """,
        ),
    )
    gate = asyncio.Event()

    async def slow_run(name: str, task: str) -> None:
        await gate.wait()

    bcast: list[tuple[str, dict]] = []
    sched = TriggerScheduler(
        registry=reg, hub=Hub(), agent_run=slow_run,
        broadcast=_bcast_capture(bcast),
    )
    first = asyncio.create_task(sched.fire_now("daily"))
    # Give the first fire a tick to acquire the lock.
    await asyncio.sleep(0.01)
    second_summary = await sched.fire_now("daily")
    assert second_summary["status"] == "skipped"
    gate.set()
    await first
