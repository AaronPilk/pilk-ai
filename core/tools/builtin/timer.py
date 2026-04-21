"""timer_set — "ping me in N minutes about X".

Lightweight. Inserts a single row into the ``timers`` table and hands
the rest to :class:`core.timers.daemon.TimerDaemon`. Returns the
timer id so the orchestrator (or a later tool turn) can cancel it by
id if the operator changes their mind.

### Risk posture

WRITE_LOCAL. The store lives in SQLite on the operator's machine —
nothing is sent until the fire itself, and the fire path is
best-effort Telegram push (COMMS at that layer, but it's the
daemon's concern, not this tool's).

Setting a timer doesn't itself notify anyone, so forcing an approval
prompt here would defeat the operator's intent ("set a timer for 10
minutes"). The COMMS check lands on the fire's Telegram push, which
a future build can wire through the approval queue if we want it
stricter — for V1 the fire is unconditional since the operator
asked for it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.logging import get_logger
from core.policy.risk import RiskClass
from core.timers.store import MAX_TIMER_MINUTES, TimerStore
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.tools.timer")


def make_timer_set_tool(store: TimerStore) -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        minutes_raw = args.get("minutes")
        try:
            minutes = int(minutes_raw)
        except (TypeError, ValueError):
            return ToolOutcome(
                content=(
                    "timer_set requires 'minutes' as a positive "
                    "integer."
                ),
                is_error=True,
            )
        if minutes <= 0:
            return ToolOutcome(
                content="timer_set 'minutes' must be > 0.",
                is_error=True,
            )
        if minutes > MAX_TIMER_MINUTES:
            return ToolOutcome(
                content=(
                    f"timer_set 'minutes' too large ({minutes} > "
                    f"{MAX_TIMER_MINUTES}). For anything past a day "
                    "use a cron trigger instead — timers are for "
                    "short reminders."
                ),
                is_error=True,
            )
        message = str(args.get("message") or "").strip()
        if not message:
            return ToolOutcome(
                content=(
                    "timer_set requires a 'message' — the label the "
                    "operator sees when it fires."
                ),
                is_error=True,
            )
        fires_at = datetime.now(UTC) + timedelta(minutes=minutes)
        try:
            timer = await store.create(
                fires_at=fires_at,
                message=message,
                source="tool",
            )
        except ValueError as e:
            return ToolOutcome(content=str(e), is_error=True)
        return ToolOutcome(
            content=(
                f"Timer set for {minutes} min from now "
                f"({timer.fires_at[:19]}): {message}. "
                f"Cancel with id={timer.id}."
            ),
            data={
                "id": timer.id,
                "fires_at": timer.fires_at,
                "minutes": minutes,
                "message": timer.message,
            },
        )

    return Tool(
        name="timer_set",
        description=(
            "Schedule a one-shot reminder to ping the operator in N "
            "minutes. Use for 'remind me in 10 min to check the "
            "oven' style asks — ephemeral, operator-invoked. Fires "
            f"deliver a Telegram push (best-effort) + a hub event. "
            f"Cap {MAX_TIMER_MINUTES} min; for longer horizons or "
            "recurring schedules, use a cron trigger. Returns the "
            "timer id so you can cancel it if the operator changes "
            "their mind (DELETE /timers/{id})."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TIMER_MINUTES,
                    "description": (
                        "Minutes from now until the timer fires."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": (
                        "What the timer is about — the label the "
                        "operator sees when it fires. Keep it short "
                        "(fits in a Telegram notification)."
                    ),
                },
            },
            "required": ["minutes", "message"],
        },
        risk=RiskClass.WRITE_LOCAL,
        handler=handler,
    )


__all__ = ["make_timer_set_tool"]
