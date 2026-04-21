"""TimerDaemon — polls the timers table + delivers due fires.

One background task per daemon. Ticks every ~30s, queries
``TimerStore.due_now()``, and for each due row:

  1. Atomically claims it via ``mark_fired`` (race-safe if two
     pollers ever exist).
  2. Broadcasts ``timer.fired`` on the hub so dashboard + activity
     feed reflect the fire instantly.
  3. Pushes a Telegram message to the operator (best-effort).

The daemon intentionally does NOT invoke agents on fire. Timers are
"ping me about X" reminders; "run my morning_inbox_triage agent" is
the trigger scheduler's job. Keeping the contracts distinct prevents
timers from growing into a second agent-runner.

No Telegram config + no macOS? The broadcast still fires and the row
still gets marked fired — the operator just sees it in the dashboard
Activity feed instead of on their phone. Cheap graceful degradation.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from core.integrations.telegram import TelegramClient, TelegramError
from core.logging import get_logger
from core.timers.store import Timer, TimerStore

log = get_logger("pilkd.timers.daemon")

# How often the daemon re-queries the store. Timers are a "reminders"
# feature, not a hard-real-time scheduler, so 30s drift is fine and
# the query is ~O(1) with the partial index on fires_at.
DEFAULT_POLL_SECONDS = 30.0

BroadcastFn = Callable[[str, dict[str, Any]], Awaitable[None]]
TelegramClientFn = Callable[[], TelegramClient | None]


class TimerDaemon:
    """Construct once per pilkd; ``start()`` in lifespan startup,
    ``stop()`` in shutdown.

    ``telegram_client_fn`` resolves the live client on each fire so
    that an operator who adds / swaps Telegram credentials after
    boot sees the effect on the next fire (no daemon restart
    required).
    """

    def __init__(
        self,
        *,
        store: TimerStore,
        broadcast: BroadcastFn,
        telegram_client_fn: TelegramClientFn,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._broadcast = broadcast
        self._telegram_client_fn = telegram_client_fn
        self._poll_s = float(poll_seconds)
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="timer-daemon")
        log.info("timer_daemon_started", poll_s=self._poll_s)

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=self._poll_s + 5)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        log.info("timer_daemon_stopped")

    # ── main loop ────────────────────────────────────────────────

    async def _run(self) -> None:
        # One eager tick at start so an operator who sets a 1-minute
        # timer and restarts doesn't have to wait a full poll cycle.
        await self._tick()
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._poll_s,
                )
            if self._stop.is_set():
                return
            await self._tick()

    async def _tick(self) -> None:
        try:
            due = await self._store.due_now(self._now())
        except Exception as e:
            # SQLite blip shouldn't kill the daemon — log + keep ticking.
            log.warning("timer_due_query_failed", error=str(e))
            return
        for pending in due:
            claimed = await self._store.mark_fired(pending.id)
            if claimed is None:
                # Another poller beat us to this row. Should never
                # happen with one daemon per pilkd but defensive.
                continue
            await self._deliver(claimed)

    # ── delivery ─────────────────────────────────────────────────

    async def _deliver(self, timer: Timer) -> None:
        payload = {
            "id": timer.id,
            "message": timer.message,
            "fires_at": timer.fires_at,
            "fired_at": timer.fired_at,
            "source": timer.source,
        }
        await self._broadcast("timer.fired", payload)
        log.info(
            "timer_fired",
            id=timer.id,
            fires_at=timer.fires_at,
            source=timer.source,
        )
        try:
            client = self._telegram_client_fn()
        except Exception as e:
            log.warning("timer_telegram_client_error", error=str(e))
            return
        if client is None:
            return
        text = f"⏰ {timer.message}"
        try:
            await client.send_message(text)
        except TelegramError as e:
            log.warning(
                "timer_telegram_push_failed",
                id=timer.id, status=e.status, message=e.message,
            )
        except Exception as e:
            log.warning(
                "timer_telegram_push_error", id=timer.id, error=str(e),
            )


__all__ = ["DEFAULT_POLL_SECONDS", "TimerDaemon"]
