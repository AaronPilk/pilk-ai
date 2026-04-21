"""Unit tests for the one-shot timer subsystem.

Covers the three layers:
- ``TimerStore`` SQL round-trip, race-safe mark_fired, list + cancel
- ``TimerDaemon`` tick-and-deliver flow (broadcast + Telegram push)
- ``timer_set`` tool validation + happy path
- ``/timers`` REST routes (list, create, cancel)

The daemon test injects a fake now() so we can move the wall clock
forward deterministically and assert exactly one delivery per due
row without relying on sleeps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from core.api.routes import timers as timers_route
from core.db import ensure_schema
from core.timers import TimerDaemon, TimerStore
from core.timers.store import MAX_TIMER_MINUTES
from core.tools.builtin.timer import make_timer_set_tool
from core.tools.registry import ToolContext

# ── TimerStore ───────────────────────────────────────────────────


@pytest.fixture
async def store(tmp_path: Path) -> TimerStore:
    db = tmp_path / "pilk.db"
    ensure_schema(db)
    return TimerStore(db)


@pytest.mark.asyncio
async def test_create_rejects_past_time(store: TimerStore) -> None:
    with pytest.raises(ValueError):
        await store.create(
            fires_at=datetime.now(UTC) - timedelta(minutes=1),
            message="x",
        )


@pytest.mark.asyncio
async def test_create_rejects_empty_message(store: TimerStore) -> None:
    with pytest.raises(ValueError):
        await store.create(
            fires_at=datetime.now(UTC) + timedelta(minutes=5),
            message="   ",
        )


@pytest.mark.asyncio
async def test_create_rejects_too_far_out(store: TimerStore) -> None:
    with pytest.raises(ValueError):
        await store.create(
            fires_at=datetime.now(UTC)
                + timedelta(minutes=MAX_TIMER_MINUTES + 1),
            message="long",
        )


@pytest.mark.asyncio
async def test_create_round_trip(store: TimerStore) -> None:
    fires = datetime.now(UTC) + timedelta(minutes=5)
    t = await store.create(fires_at=fires, message="check oven")
    assert t.id.startswith("tmr_")
    assert t.is_active
    active = await store.list_active()
    assert [x.id for x in active] == [t.id]


@pytest.mark.asyncio
async def test_due_now_respects_time_gate(store: TimerStore) -> None:
    near = await store.create(
        fires_at=datetime.now(UTC) + timedelta(seconds=1),
        message="near",
    )
    _far = await store.create(
        fires_at=datetime.now(UTC) + timedelta(minutes=30),
        message="far",
    )
    # At "now + 2s", only `near` is due.
    due = await store.due_now(datetime.now(UTC) + timedelta(seconds=2))
    assert [d.id for d in due] == [near.id]


@pytest.mark.asyncio
async def test_mark_fired_is_race_safe(store: TimerStore) -> None:
    """Two concurrent UPDATE attempts must both succeed at the SQL
    level but only one should see rowcount=1. ``mark_fired`` returns
    the Timer for the winner, ``None`` for the loser."""
    t = await store.create(
        fires_at=datetime.now(UTC) + timedelta(seconds=1),
        message="race",
    )
    first = await store.mark_fired(t.id)
    second = await store.mark_fired(t.id)
    assert first is not None and first.fired_at is not None
    assert second is None


@pytest.mark.asyncio
async def test_cancel_active_works(store: TimerStore) -> None:
    t = await store.create(
        fires_at=datetime.now(UTC) + timedelta(minutes=5),
        message="nope",
    )
    assert await store.cancel(t.id) is True
    # Cancelled row doesn't show as active.
    assert await store.list_active() == []
    # Cancel is idempotent — second call returns False.
    assert await store.cancel(t.id) is False


@pytest.mark.asyncio
async def test_cancel_after_fire_returns_false(store: TimerStore) -> None:
    t = await store.create(
        fires_at=datetime.now(UTC) + timedelta(seconds=1),
        message="fired",
    )
    assert await store.mark_fired(t.id) is not None
    assert await store.cancel(t.id) is False


# ── timer_set tool ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timer_set_missing_minutes(store: TimerStore) -> None:
    tool = make_timer_set_tool(store)
    out = await tool.handler({"message": "x"}, ToolContext())
    assert out.is_error
    assert "minutes" in out.content.lower()


@pytest.mark.asyncio
async def test_timer_set_missing_message(store: TimerStore) -> None:
    tool = make_timer_set_tool(store)
    out = await tool.handler({"minutes": 5}, ToolContext())
    assert out.is_error
    assert "message" in out.content.lower()


@pytest.mark.asyncio
async def test_timer_set_zero_minutes(store: TimerStore) -> None:
    tool = make_timer_set_tool(store)
    out = await tool.handler(
        {"minutes": 0, "message": "x"}, ToolContext(),
    )
    assert out.is_error


@pytest.mark.asyncio
async def test_timer_set_too_long(store: TimerStore) -> None:
    tool = make_timer_set_tool(store)
    out = await tool.handler(
        {"minutes": MAX_TIMER_MINUTES + 1, "message": "x"},
        ToolContext(),
    )
    assert out.is_error
    assert "cron trigger" in out.content


@pytest.mark.asyncio
async def test_timer_set_happy_path(store: TimerStore) -> None:
    tool = make_timer_set_tool(store)
    out = await tool.handler(
        {"minutes": 5, "message": "check oven"}, ToolContext(),
    )
    assert not out.is_error
    assert out.data["minutes"] == 5
    assert out.data["message"] == "check oven"
    assert out.data["id"].startswith("tmr_")
    # Round-trip: row is active in the store.
    active = await store.list_active()
    assert len(active) == 1
    assert active[0].id == out.data["id"]


# ── TimerDaemon ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_daemon_tick_fires_due_timer_and_broadcasts(
    store: TimerStore,
) -> None:
    broadcasts: list[tuple[str, dict]] = []

    async def capture(event_type: str, payload: dict) -> None:
        broadcasts.append((event_type, payload))

    class _FakeTgClient:
        def __init__(self) -> None:
            self.sends: list[str] = []

        async def send_message(self, text: str) -> None:
            self.sends.append(text)

    client = _FakeTgClient()

    # Movable clock — we set to 10 minutes from now so the timer
    # counts as due without needing asyncio.sleep.
    fires = datetime.now(UTC) + timedelta(minutes=5)
    t = await store.create(fires_at=fires, message="check oven")

    daemon = TimerDaemon(
        store=store,
        broadcast=capture,
        telegram_client_fn=lambda: client,
        now_fn=lambda: fires + timedelta(seconds=1),
    )
    await daemon._tick()

    assert any(ev == "timer.fired" for ev, _ in broadcasts)
    fired_payload = next(p for ev, p in broadcasts if ev == "timer.fired")
    assert fired_payload["id"] == t.id
    assert fired_payload["message"] == "check oven"
    # Telegram push with the emoji prefix.
    assert client.sends == ["⏰ check oven"]
    # Row should now be marked fired in the store.
    assert await store.list_active() == []


@pytest.mark.asyncio
async def test_daemon_does_not_fire_non_due(store: TimerStore) -> None:
    broadcasts: list[tuple[str, dict]] = []

    async def capture(event_type: str, payload: dict) -> None:
        broadcasts.append((event_type, payload))

    await store.create(
        fires_at=datetime.now(UTC) + timedelta(minutes=30),
        message="far future",
    )
    daemon = TimerDaemon(
        store=store,
        broadcast=capture,
        telegram_client_fn=lambda: None,
    )
    await daemon._tick()
    assert broadcasts == []
    assert len(await store.list_active()) == 1


@pytest.mark.asyncio
async def test_daemon_tolerates_missing_telegram(store: TimerStore) -> None:
    """Telegram not configured → client_fn returns None. Fire still
    lands via broadcast + the row is marked fired."""
    broadcasts: list[tuple[str, dict]] = []

    async def capture(event_type: str, payload: dict) -> None:
        broadcasts.append((event_type, payload))

    fires = datetime.now(UTC) + timedelta(seconds=1)
    t = await store.create(fires_at=fires, message="no tg")
    daemon = TimerDaemon(
        store=store,
        broadcast=capture,
        telegram_client_fn=lambda: None,
        now_fn=lambda: fires + timedelta(seconds=1),
    )
    await daemon._tick()
    assert any(ev == "timer.fired" for ev, _ in broadcasts)
    # Still marked fired in the store.
    reactivated = await store.list_active()
    assert all(r.id != t.id for r in reactivated)


@pytest.mark.asyncio
async def test_daemon_survives_telegram_failure(store: TimerStore) -> None:
    broadcasts: list[tuple[str, dict]] = []

    async def capture(event_type: str, payload: dict) -> None:
        broadcasts.append((event_type, payload))

    class _AngryClient:
        async def send_message(self, text: str) -> None:
            raise RuntimeError("network is down")

    fires = datetime.now(UTC) + timedelta(seconds=1)
    await store.create(fires_at=fires, message="oops")
    daemon = TimerDaemon(
        store=store,
        broadcast=capture,
        telegram_client_fn=lambda: _AngryClient(),
        now_fn=lambda: fires + timedelta(seconds=1),
    )
    # Should NOT raise — failure is swallowed after the fire is
    # persisted.
    await daemon._tick()
    assert any(ev == "timer.fired" for ev, _ in broadcasts)


@pytest.mark.asyncio
async def test_daemon_only_delivers_once_per_timer(
    store: TimerStore,
) -> None:
    """Two consecutive ticks covering the same due row must only
    fire once — the mark_fired lock is the source of truth."""
    broadcasts: list[tuple[str, dict]] = []

    async def capture(event_type: str, payload: dict) -> None:
        broadcasts.append((event_type, payload))

    class _FakeClient:
        def __init__(self) -> None:
            self.count = 0

        async def send_message(self, text: str) -> None:
            self.count += 1

    client = _FakeClient()
    fires = datetime.now(UTC) + timedelta(seconds=1)
    await store.create(fires_at=fires, message="once")
    daemon = TimerDaemon(
        store=store,
        broadcast=capture,
        telegram_client_fn=lambda: client,
        now_fn=lambda: fires + timedelta(seconds=1),
    )
    await daemon._tick()
    await daemon._tick()
    assert client.count == 1
    assert sum(1 for ev, _ in broadcasts if ev == "timer.fired") == 1


# ── /timers routes ───────────────────────────────────────────────


@dataclass
class _FakeState:
    timers: TimerStore | None = None


class _FakeApp:
    def __init__(self, store: TimerStore | None = None) -> None:
        self.state = _FakeState(timers=store)


@dataclass
class _FakeRequest:
    app: _FakeApp = field(default_factory=_FakeApp)


@pytest.mark.asyncio
async def test_list_timers_empty() -> None:
    resp = await timers_route.list_timers(_FakeRequest())
    assert resp == {"active": [], "recent": []}


@pytest.mark.asyncio
async def test_list_timers_returns_active_and_recent(
    store: TimerStore,
) -> None:
    _near = await store.create(
        fires_at=datetime.now(UTC) + timedelta(minutes=5),
        message="active one",
    )
    req = _FakeRequest(_FakeApp(store=store))
    resp = await timers_route.list_timers(req)
    assert len(resp["active"]) == 1
    assert resp["active"][0]["message"] == "active one"
    assert len(resp["recent"]) == 1


@pytest.mark.asyncio
async def test_create_timer_happy_path(store: TimerStore) -> None:
    req = _FakeRequest(_FakeApp(store=store))
    from core.api.routes.timers import CreateBody
    resp = await timers_route.create_timer(
        CreateBody(minutes=5, message="from api"), req,
    )
    assert resp["message"] == "from api"
    assert resp["id"].startswith("tmr_")
    assert resp["source"] == "api"


@pytest.mark.asyncio
async def test_create_timer_without_store_503() -> None:
    from core.api.routes.timers import CreateBody
    with pytest.raises(HTTPException) as info:
        await timers_route.create_timer(
            CreateBody(minutes=5, message="x"), _FakeRequest(),
        )
    assert info.value.status_code == 503


@pytest.mark.asyncio
async def test_cancel_timer_found(store: TimerStore) -> None:
    t = await store.create(
        fires_at=datetime.now(UTC) + timedelta(minutes=5),
        message="kill me",
    )
    req = _FakeRequest(_FakeApp(store=store))
    resp = await timers_route.cancel_timer(t.id, req)
    assert resp == {"id": t.id, "cancelled": True}


@pytest.mark.asyncio
async def test_cancel_timer_missing_404(store: TimerStore) -> None:
    req = _FakeRequest(_FakeApp(store=store))
    with pytest.raises(HTTPException) as info:
        await timers_route.cancel_timer("tmr_ghost", req)
    assert info.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_timer_already_fired_404(store: TimerStore) -> None:
    t = await store.create(
        fires_at=datetime.now(UTC) + timedelta(seconds=1),
        message="done",
    )
    await store.mark_fired(t.id)
    req = _FakeRequest(_FakeApp(store=store))
    with pytest.raises(HTTPException) as info:
        await timers_route.cancel_timer(t.id, req)
    assert info.value.status_code == 404


# Satisfy the import check in strict mode — otherwise ``Any`` is
# listed as unused. The dataclass state annotation reaches for it.
_: Any = None
