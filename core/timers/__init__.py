"""Lightweight one-shot timers — persistent and restart-resilient.

A timer is a single future reminder: "ping me in 10 minutes about the
oven." The operator (or an agent) sets one via the ``timer_set`` tool,
and the :class:`TimerDaemon` delivers a Telegram push + hub broadcast
when the wall clock reaches the fire time.

### Why its own subsystem (not the trigger scheduler)

Triggers are manifest-curated stable schedules. Timers are ephemeral,
operator-invoked, one-shot. Merging them would force either:

- Writing a YAML manifest for every timer (clutters the repo), or
- Letting the trigger scheduler fire arbitrary Python callbacks
  instead of just agents (broadens its contract)

Neither is worth it. The timer subsystem is ~150 LoC total and shares
the scheduler's lifecycle + broadcast pattern without sharing code.

### Restart resilience

Timers persist to SQLite (``timers`` table). On daemon restart, the
poll loop picks up everything with ``fires_at <= now AND fired_at IS
NULL``. An operator who sets a 30-minute timer then restarts pilkd
at minute 10 still gets pinged at minute 30 (or as soon as the
daemon comes back up, whichever is later).

### Delivery

When a timer fires:

1. Mark ``fired_at`` in SQLite (idempotency — same timer can't fire
   twice if two pollers race).
2. Broadcast ``timer.fired`` on the hub so the dashboard / activity
   feed surfaces it.
3. Push a Telegram notification if the bot is configured — that's
   how the operator actually gets notified when away from the
   dashboard.
4. On macOS, optionally pop a native notification via osascript.

Failures at step 3 or 4 are logged but never block the fire sequence
— the SQLite mark + hub broadcast are the source of truth; Telegram
is best-effort push.
"""

from __future__ import annotations

from core.timers.daemon import TimerDaemon
from core.timers.store import Timer, TimerStore

__all__ = ["Timer", "TimerDaemon", "TimerStore"]
