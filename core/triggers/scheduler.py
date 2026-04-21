"""Proactive trigger scheduler.

One background task per daemon. Evaluates cron triggers once a minute
and subscribes to the hub for event triggers. On fire, calls
``orchestrator.agent_run(agent_name, goal)`` — same entry point the
REST ``/agents/{name}/run`` endpoint uses, so the orchestrator's
serialization lock + plan creation + broadcast all behave identically
whether a human or the clock kicked it off.

Defensive posture:

- One fire at a time. If the orchestrator is busy (another plan
  running, including a previous trigger's fire), we skip this tick
  and broadcast ``trigger.skipped`` with the reason. Queuing would
  mean the operator comes back to dozens of stale "last hour" runs
  after a long task. Better to miss and let the next tick pick up
  or let the operator re-fire manually.
- Cron fires at minute resolution but the daemon ticks every ~60s.
  To avoid firing a trigger twice in the same wall-clock minute if
  the previous tick slipped, we track ``last_tick_minute`` and only
  evaluate once per boundary.
- Disabled triggers stay registered but never fire. Toggling via
  the UI hits the registry directly; the scheduler re-reads on
  every tick, so there's no cache to invalidate.
- An event-trigger whose filter matches fires inline from the hub
  broadcast. We don't spawn a task per fire; instead we call
  ``orchestrator.agent_run`` through ``asyncio.create_task`` with
  a proper exception handler so a broken agent can't stall the
  hub's fanout.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.logging import get_logger
from core.triggers.manifest import (
    CronScheduleSpec,
    EventScheduleSpec,
    TriggerManifest,
)
from core.triggers.registry import TriggerRegistry

if TYPE_CHECKING:
    # Avoid importing core.api.hub at runtime — the eager
    # ``core/api/__init__.py`` transitively loads ``core.api.app``,
    # which imports this module, and the cycle collapses. We only need
    # the type for annotations (turned into strings by
    # ``from __future__ import annotations``), so this is sufficient.
    from core.api.hub import Hub

log = get_logger("pilkd.triggers.scheduler")

# Cron evaluation cadence. We target one tick a minute; a shorter
# cadence wastes CPU, a longer one risks missing a minute when the
# wall clock drifts across a boundary.
DEFAULT_TICK_SECONDS = 30.0
# Wall-clock budget for one agent run kicked off by a trigger. Same
# shape as the Telegram bridge — an overly patient scheduler becomes
# a stuck scheduler.
AGENT_RUN_TIMEOUT_S = 60 * 60.0  # 1h

AgentRunFn = Callable[[str, str], Awaitable[None]]
BroadcastFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class TriggerScheduler:
    """Background daemon that fires triggers on schedule + event.

    Construct once per daemon. :meth:`start` in lifespan startup,
    :meth:`stop` in lifespan shutdown. Safe to call :meth:`fire_now`
    from a REST route to trigger a manual one-shot.
    """

    def __init__(
        self,
        *,
        registry: TriggerRegistry,
        hub: Hub,
        agent_run: AgentRunFn,
        broadcast: BroadcastFn,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = registry
        self._hub = hub
        self._agent_run = agent_run
        self._broadcast = broadcast
        self._tick_s = float(tick_seconds)
        # Injectable clock so tests can drive the cron tick deterministically.
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._fire_lock = asyncio.Lock()
        # Hold strong refs to in-flight event-triggered tasks so the
        # runtime doesn't GC them mid-run. Discarded on completion.
        self._event_tasks: set[asyncio.Task] = set()
        # Minute of the last evaluated cron tick, keyed on the
        # (year, month, day, hour, minute) tuple so we never evaluate
        # the same wall-clock minute twice.
        self._last_tick_minute: tuple[int, int, int, int, int] | None = None

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._hub.subscribe(self._on_event)
        self._task = asyncio.create_task(self._run(), name="trigger-scheduler")
        log.info(
            "trigger_scheduler_started",
            tick_s=self._tick_s,
            triggers=sorted(self._registry.manifests().keys()),
        )

    async def stop(self) -> None:
        self._stop.set()
        self._hub.unsubscribe(self._on_event)
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=self._tick_s + 5)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        log.info("trigger_scheduler_stopped")

    # ── cron tick loop ───────────────────────────────────────────────

    async def _run(self) -> None:
        # One evaluation immediately at start so a trigger that matches
        # the boot minute doesn't wait a full tick to fire.
        await self._tick()
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_s)
            if self._stop.is_set():
                return
            await self._tick()

    async def _tick(self) -> None:
        now = self._now()
        minute_key = (now.year, now.month, now.day, now.hour, now.minute)
        if minute_key == self._last_tick_minute:
            return
        self._last_tick_minute = minute_key
        for manifest in list(self._registry.iter_enabled()):
            schedule = manifest.schedule
            if not isinstance(schedule, CronScheduleSpec):
                continue
            from core.triggers.cron import parse_cron
            # Cron string is validated at manifest-parse time, so
            # this can't raise — the try/except guards against a
            # misconfigured YAML slipping past validation anyway.
            try:
                cron = parse_cron(schedule.expression)
            except Exception as e:
                log.warning(
                    "trigger_cron_parse_failed",
                    name=manifest.name,
                    error=str(e),
                )
                continue
            if cron.matches(now):
                await self._fire(
                    manifest,
                    source="cron",
                    context={"expression": schedule.expression},
                )

    # ── event trigger dispatch ───────────────────────────────────────

    async def _on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        # Avoid firing a trigger from its OWN plan broadcast — the
        # orchestrator emits events like ``plan.created`` and
        # ``chat.assistant`` for every run, and a misconfigured
        # event trigger could loop. Cheap pre-filter on the
        # ``trigger.*`` prefix stops the recursion dead; legitimate
        # triggers that care about trigger events can still use a
        # more specific filter.
        if event_type.startswith("trigger."):
            return
        for manifest in list(self._registry.iter_enabled()):
            schedule = manifest.schedule
            if not isinstance(schedule, EventScheduleSpec):
                continue
            if schedule.event_type != event_type:
                continue
            if not _filter_matches(schedule.filter, payload):
                continue
            # Fire-and-forget the agent run so the hub's listener loop
            # keeps moving — one slow agent can't delay sibling
            # listeners (Sentinel, dashboard fanout, etc.). We hold a
            # strong ref in ``_event_tasks`` so the runtime doesn't GC
            # the task before it finishes.
            task = asyncio.create_task(
                self._fire(
                    manifest,
                    source="event",
                    context={
                        "event_type": event_type,
                        "payload_keys": sorted(payload.keys()),
                    },
                ),
                name=f"trigger-fire-{manifest.name}",
            )
            self._event_tasks.add(task)
            task.add_done_callback(self._event_tasks.discard)

    # ── manual one-shot (REST handler uses this) ─────────────────────

    async def fire_now(self, name: str) -> dict[str, Any]:
        manifest = self._registry.get(name)
        return await self._fire(
            manifest, source="manual", context={"manual": True}
        )

    # ── the one fire path ────────────────────────────────────────────

    async def _fire(
        self,
        manifest: TriggerManifest,
        *,
        source: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Attempt to run the trigger.

        Returns a summary dict so the REST + UI can tell the operator
        exactly what happened (fired / skipped / failed). We DON'T
        raise on failure — a broken agent should not crash the
        scheduler loop.
        """
        if self._fire_lock.locked():
            reason = "another trigger is firing"
            await self._broadcast(
                "trigger.skipped",
                {
                    "name": manifest.name,
                    "source": source,
                    "reason": reason,
                    **context,
                },
            )
            log.info(
                "trigger_skipped",
                name=manifest.name, source=source, reason=reason,
            )
            return {"status": "skipped", "reason": reason}

        async with self._fire_lock:
            fired_at = await self._registry.mark_fired(manifest.name)
            await self._broadcast(
                "trigger.fired",
                {
                    "name": manifest.name,
                    "agent_name": manifest.agent_name,
                    "goal": manifest.goal,
                    "source": source,
                    "fired_at": fired_at,
                    **context,
                },
            )
            log.info(
                "trigger_fired",
                name=manifest.name,
                agent=manifest.agent_name,
                source=source,
            )
            try:
                await asyncio.wait_for(
                    self._agent_run(manifest.agent_name, manifest.goal),
                    timeout=AGENT_RUN_TIMEOUT_S,
                )
                return {"status": "fired", "fired_at": fired_at}
            except Exception as e:
                log.warning(
                    "trigger_run_failed",
                    name=manifest.name,
                    agent=manifest.agent_name,
                    error=str(e),
                )
                await self._broadcast(
                    "trigger.failed",
                    {
                        "name": manifest.name,
                        "agent_name": manifest.agent_name,
                        "error": str(e),
                        "source": source,
                    },
                )
                return {"status": "failed", "error": str(e)}


# ── helpers ──────────────────────────────────────────────────────────


def _filter_matches(
    filter_spec: dict[str, object], payload: dict[str, Any],
) -> bool:
    """Exact-match filter on the top level of ``payload``.

    Missing key → mismatch (conservative). Value comparison is the
    usual Python ``==``, so ints compare numerically and strings
    compare case-sensitively. Flat only — dotted paths land in a
    follow-up if the need shows up.
    """
    for key, expected in filter_spec.items():
        if key not in payload:
            return False
        if payload[key] != expected:
            return False
    return True


__all__ = [
    "AGENT_RUN_TIMEOUT_S",
    "DEFAULT_TICK_SECONDS",
    "TriggerScheduler",
]
