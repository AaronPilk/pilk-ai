"""IntelligenceDaemon — optional background poller.

**Default-off.** Controlled exclusively by the
``PILK_INTELLIGENCE_DAEMON_ENABLED`` env var. When disabled, the
constructor still runs (so the FastAPI app boots cleanly) but
``start()`` is a no-op — no background task, no fetches, no clock
work. The daemon literally does nothing until the operator opts in.

When enabled, the daemon:
  - Wakes every ``tick_seconds`` (default 60s)
  - Looks for sources whose ``last_checked_at + poll_interval_seconds``
    is in the past AND ``enabled = 1`` AND ``mute_until`` is unset or
    in the past
  - Applies exponential backoff: a source with N consecutive failures
    above the threshold gets its effective interval doubled until it
    recovers
  - Fires up to ``max_concurrent`` fetches in parallel
  - Hands each fetch to :class:`IntelligencePipeline` (same code
    path as the manual refresh endpoint)
  - Never sends notifications, never fires plans, never alerts the
    operator. Storage is the only side effect.

Mirrors the lifespan + asyncio task patterns used by
``TimerDaemon`` and the trigger scheduler — same start/stop dance.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

from core.intelligence.models import SourceSpec
from core.intelligence.pipeline import IntelligencePipeline
from core.intelligence.sources import SourceRegistry
from core.logging import get_logger

log = get_logger("pilkd.intelligence.daemon")


class IntelligenceDaemon:
    def __init__(
        self,
        *,
        sources: SourceRegistry,
        pipeline: IntelligencePipeline,
        tick_seconds: int = 60,
        max_concurrent: int = 4,
        backoff_after_failures: int = 5,
        enabled: bool = False,
    ) -> None:
        # Floor the tick at 30s so even an aggressive operator config
        # can't beat external hosts to death.
        self._tick = max(30, int(tick_seconds))
        self._max_concurrent = max(1, int(max_concurrent))
        self._backoff_threshold = max(1, int(backoff_after_failures))
        self._enabled = bool(enabled)
        self._sources = sources
        self._pipeline = pipeline
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._tick_count = 0
        self._last_tick_at: str | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def tick_count(self) -> int:
        return self._tick_count

    async def start(self) -> None:
        """Start the daemon's polling loop. No-op when ``enabled`` is
        False — the FastAPI lifespan calls this unconditionally and
        the daemon decides whether to actually run."""
        if not self._enabled:
            log.info("intelligence_daemon_disabled")
            return
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._loop(), name="intelligence-daemon"
        )
        log.info(
            "intelligence_daemon_started",
            tick_seconds=self._tick,
            max_concurrent=self._max_concurrent,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None
            log.info("intelligence_daemon_stopped")

    # ── loop ─────────────────────────────────────────────────────

    async def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                started = datetime.now(UTC)
                self._tick_count += 1
                self._last_tick_at = started.isoformat()
                try:
                    await self._tick_once(now=started)
                except Exception as e:  # noqa: BLE001 — defensive
                    log.exception(
                        "intelligence_daemon_tick_failed",
                        error=str(e),
                    )
                # Sleep until the next tick; ``stop`` short-circuits.
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._tick,
                    )
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _tick_once(self, *, now: datetime) -> None:
        all_sources = await self._sources.list_sources(enabled_only=True)
        due = [s for s in all_sources if self._is_due(s, now=now)]
        if not due:
            return
        log.info(
            "intelligence_daemon_tick",
            tick=self._tick_count,
            candidates=len(all_sources),
            due=len(due),
        )
        await self._run_due(due)

    async def _run_due(self, sources: Iterable[SourceSpec]) -> None:
        sem = asyncio.Semaphore(self._max_concurrent)

        async def _runner(src: SourceSpec) -> None:
            async with sem:
                outcome = await self._pipeline.run_source(src)
                log.info(
                    "intelligence_daemon_source_done",
                    source_id=src.id,
                    slug=src.slug,
                    ok=outcome.ok,
                    items_new=outcome.items_new,
                    items_dup=outcome.items_dup,
                    items_brain_written=outcome.items_brain_written,
                    error=outcome.error,
                )

        await asyncio.gather(
            *[_runner(s) for s in sources], return_exceptions=False,
        )

    # ── helpers ──────────────────────────────────────────────────

    def _is_due(self, source: SourceSpec, *, now: datetime) -> bool:
        # Manual sources are operator-curated; the daemon never polls
        # them. Items only enter via POST /intelligence/sources/<id>
        # /items. Skipping here is the cheapest enforcement — the
        # fetcher dispatcher also refuses (defence in depth).
        if source.kind == "manual":
            return False
        # Respect the mute window.
        if source.mute_until:
            mute = self._parse_iso(source.mute_until)
            if mute is not None and mute > now:
                return False

        last = self._parse_iso(source.last_checked_at)
        # Apply exponential backoff after the configured threshold of
        # consecutive failures. Each extra failure beyond the
        # threshold doubles the effective interval (cap 6h) so a
        # busted source doesn't hammer.
        interval = source.poll_interval_seconds
        excess = source.consecutive_failures - self._backoff_threshold + 1
        if excess > 0:
            interval = min(int(interval * (2**excess)), 6 * 3600)

        if last is None:
            return True
        return now >= last + timedelta(seconds=interval)

    @staticmethod
    def _parse_iso(raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            ts = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except ValueError:
            return None


def build_daemon(
    *,
    db_path: Path,
    settings,
    pipeline: IntelligencePipeline,
    sources: SourceRegistry,
) -> IntelligenceDaemon:
    """Factory that wires settings → daemon. Lives here so
    ``core/api/app.py`` doesn't have to know the constructor's
    field names."""
    return IntelligenceDaemon(
        sources=sources,
        pipeline=pipeline,
        tick_seconds=settings.intelligence_daemon_tick_seconds,
        max_concurrent=settings.intelligence_daemon_max_concurrent,
        backoff_after_failures=settings.intelligence_failure_backoff_after,
        enabled=settings.intelligence_daemon_enabled,
    )


__all__ = ["IntelligenceDaemon", "build_daemon"]
