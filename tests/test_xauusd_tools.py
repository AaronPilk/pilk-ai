"""Tool-layer tests for the XAU/USD execution agent.

Covers:

- Symbol whitelist enforcement (any non-XAUUSD symbol refuses loud).
- LIVE_TRADING_ENABLED hard gate (place_order refuses in paper).
- State-machine tool actions (get, transition, disable).
- Evaluate + calc_size end-to-end on a synthetic uptrend.
- flatten_all always force-disables the state machine.
"""

from __future__ import annotations

import pytest

from core.tools.builtin.xauusd import (
    reset_state_for_tests,
    xauusd_calc_size_tool,
    xauusd_evaluate_tool,
    xauusd_flatten_all_tool,
    xauusd_place_order_tool,
    xauusd_state_tool,
)
from core.tools.registry import ToolContext


@pytest.fixture(autouse=True)
def _reset_state():
    reset_state_for_tests()
    yield
    reset_state_for_tests()


def _uptrend_dicts(n: int, start: float = 2400.0, step: float = 0.8) -> list[dict]:
    rows: list[dict] = []
    price = start
    for i in range(n):
        o = price
        c = price + step
        rows.append(
            {"ts": i * 300, "o": o, "h": c + 0.2, "l": o - 0.15, "c": c}
        )
        price = c
    return rows


# ── Symbol whitelist ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_refuses_non_xauusd() -> None:
    out = await xauusd_evaluate_tool.handler(
        {"symbol": "EURUSD", "candles_5m": _uptrend_dicts(220)},
        ToolContext(),
    )
    assert out.is_error
    assert "XAUUSD" in out.content


@pytest.mark.asyncio
async def test_calc_size_refuses_non_xauusd() -> None:
    out = await xauusd_calc_size_tool.handler(
        {
            "symbol": "GBPAUD",
            "equity_usd": 1000,
            "entry_price": 2400,
            "stop_price": 2397,
        },
        ToolContext(),
    )
    assert out.is_error
    assert "XAUUSD" in out.content


# ── Evaluate happy path ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_uptrend_returns_structured_verdict() -> None:
    out = await xauusd_evaluate_tool.handler(
        {
            "candles_5m": _uptrend_dicts(250),
            "candles_15m": _uptrend_dicts(80),
            "candles_1h": _uptrend_dicts(80),
            "candles_4h": _uptrend_dicts(80),
            "spread_usd": 0.20,
        },
        ToolContext(),
    )
    assert not out.is_error
    assert out.data["verdict"] == "TAKE_LONG"
    assert "LONG" in out.data["reason"]


# ── calc_size acceptance + refusal ─────────────────────────────────


@pytest.mark.asyncio
async def test_calc_size_returns_position() -> None:
    out = await xauusd_calc_size_tool.handler(
        {
            "equity_usd": 10_000,
            "entry_price": 2400,
            "stop_price": 2397,
            "spread_usd": 0.20,
        },
        ToolContext(),
    )
    assert not out.is_error
    assert out.data["lots"] == 0.16
    assert out.data["risk_usd"] == 48.0


@pytest.mark.asyncio
async def test_calc_size_refuses_tight_stop() -> None:
    out = await xauusd_calc_size_tool.handler(
        {
            "equity_usd": 10_000,
            "entry_price": 2400,
            "stop_price": 2399.5,
            "spread_usd": 0.20,
        },
        ToolContext(),
    )
    assert out.is_error
    assert out.data["refused"] is True


# ── State machine via tool ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_get_transition_disable() -> None:
    ctx = ToolContext()
    get1 = await xauusd_state_tool.handler({"action": "get"}, ctx)
    assert get1.data["state"] == "OFF"

    ok = await xauusd_state_tool.handler(
        {"action": "transition", "to": "SCANNING", "reason": "start"},
        ctx,
    )
    assert not ok.is_error
    assert ok.data["to"] == "SCANNING"

    illegal = await xauusd_state_tool.handler(
        {"action": "transition", "to": "IN_POSITION", "reason": "skip"},
        ctx,
    )
    assert illegal.is_error
    assert "illegal" in illegal.content.lower()

    killed = await xauusd_state_tool.handler(
        {"action": "disable", "reason": "operator stopped"},
        ctx,
    )
    assert not killed.is_error
    assert killed.data["state"] == "DISABLED"


@pytest.mark.asyncio
async def test_state_transition_requires_reason() -> None:
    out = await xauusd_state_tool.handler(
        {"action": "transition", "to": "SCANNING", "reason": ""},
        ToolContext(),
    )
    assert out.is_error


# ── place_order refuses in paper; flatten_all always disables ──────


@pytest.mark.asyncio
async def test_place_order_refuses_in_paper_mode() -> None:
    out = await xauusd_place_order_tool.handler(
        {
            "side": "LONG",
            "lots": 0.10,
            "entry_price": 2400,
            "stop_price": 2397,
        },
        ToolContext(),
    )
    assert out.is_error
    assert "LIVE_TRADING_ENABLED" in out.content


@pytest.mark.asyncio
async def test_flatten_all_force_disables() -> None:
    # Move into a non-OFF state first so the force-disable is a real transition.
    await xauusd_state_tool.handler(
        {"action": "transition", "to": "SCANNING", "reason": "start"},
        ToolContext(),
    )
    out = await xauusd_flatten_all_tool.handler(
        {"reason": "abort"}, ToolContext()
    )
    # Paper mode returns non-error (no live positions to close); state flips.
    assert out.data["state"] == "DISABLED"


# ── execution_mode actions on the state tool ───────────────────────


@pytest.mark.asyncio
async def test_state_get_includes_execution_mode() -> None:
    out = await xauusd_state_tool.handler({"action": "get"}, ToolContext())
    assert "execution_mode" in out.data
    # Default when store isn't wired in this test env.
    assert out.data["execution_mode"] == "approve"


@pytest.mark.asyncio
async def test_state_set_mode_without_store_surfaces_unavailable() -> None:
    # The tool test fixture doesn't wire a live store; set_mode should
    # error loudly rather than silently succeeding.
    from core.trading.xauusd.settings_store import set_xauusd_settings_store

    set_xauusd_settings_store(None)
    out = await xauusd_state_tool.handler(
        {"action": "set_mode", "mode": "autonomous"}, ToolContext()
    )
    assert out.is_error
    assert "unavailable" in out.content or "not initialized" in out.content


@pytest.mark.asyncio
async def test_state_set_mode_rejects_unknown(tmp_path) -> None:
    from core.db.migrations import ensure_schema
    from core.trading.xauusd.settings_store import (
        XAUUSDSettingsStore,
        set_xauusd_settings_store,
    )

    db = tmp_path / "pilk.db"
    ensure_schema(db)
    set_xauusd_settings_store(XAUUSDSettingsStore(db))
    try:
        out = await xauusd_state_tool.handler(
            {"action": "set_mode", "mode": "YOLO"}, ToolContext()
        )
        assert out.is_error
        assert "unknown" in out.content
    finally:
        set_xauusd_settings_store(None)


@pytest.mark.asyncio
async def test_state_set_mode_roundtrip(tmp_path) -> None:
    from core.db.migrations import ensure_schema
    from core.trading.xauusd.settings_store import (
        XAUUSDSettingsStore,
        set_xauusd_settings_store,
    )

    db = tmp_path / "pilk.db"
    ensure_schema(db)
    set_xauusd_settings_store(XAUUSDSettingsStore(db))
    try:
        setd = await xauusd_state_tool.handler(
            {"action": "set_mode", "mode": "autonomous"}, ToolContext()
        )
        assert not setd.is_error
        assert setd.data["execution_mode"] == "autonomous"

        got = await xauusd_state_tool.handler(
            {"action": "get_mode"}, ToolContext()
        )
        assert got.data["execution_mode"] == "autonomous"
    finally:
        set_xauusd_settings_store(None)


# ── get_candles wiring ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_candles_without_key_errors(monkeypatch) -> None:
    from core.config import get_settings
    from core.secrets import set_integration_secrets_store
    from core.tools.builtin.xauusd import xauusd_get_candles_tool

    # Ensure nothing is configured.
    set_integration_secrets_store(None)
    monkeypatch.setattr(get_settings(), "twelvedata_api_key", None, raising=False)

    out = await xauusd_get_candles_tool.handler(
        {"timeframe": "5M", "count": 10}, ToolContext()
    )
    assert out.is_error
    assert "Twelve Data" in out.content


# ── Broker-bound tools: account_info / open_positions / release ──


@pytest.mark.asyncio
async def test_account_info_refuses_without_broker() -> None:
    from core.tools.builtin.xauusd import xauusd_account_info_tool
    from core.trading.xauusd.broker import set_broker

    set_broker(None)
    out = await xauusd_account_info_tool.handler({}, ToolContext())
    assert out.is_error
    assert "xauusd_take_over" in out.content


@pytest.mark.asyncio
async def test_account_info_reads_from_mock_broker() -> None:
    from core.tools.builtin.xauusd import xauusd_account_info_tool
    from core.trading.xauusd.broker import MockBroker, set_broker

    set_broker(MockBroker(balance_usd=500.0, leverage=300))
    try:
        out = await xauusd_account_info_tool.handler({}, ToolContext())
        assert not out.is_error
        assert out.data["balance_usd"] == 500.0
        assert out.data["leverage"] == 300
    finally:
        set_broker(None)


@pytest.mark.asyncio
async def test_open_positions_empty_via_mock() -> None:
    from core.tools.builtin.xauusd import xauusd_open_positions_tool
    from core.trading.xauusd.broker import MockBroker, set_broker

    set_broker(MockBroker())
    try:
        out = await xauusd_open_positions_tool.handler({}, ToolContext())
        assert not out.is_error
        assert out.data["count"] == 0
    finally:
        set_broker(None)


@pytest.mark.asyncio
async def test_release_clears_adapter_and_forces_disable() -> None:
    from core.tools.builtin.xauusd import xauusd_release_tool
    from core.trading.xauusd.broker import MockBroker, set_broker
    from core.trading.xauusd.session import (
        get_attached_session,
        set_attached_session,
    )

    set_broker(MockBroker())
    set_attached_session(session_id="bb-1", account_type="demo")
    try:
        out = await xauusd_release_tool.handler(
            {"reason": "eod"}, ToolContext()
        )
        assert not out.is_error
        assert out.data["state"] == "DISABLED"
        assert get_attached_session() is None
    finally:
        from core.trading.xauusd.session import clear_attached_session

        clear_attached_session()
        set_broker(None)


# ── place_order: four gates ────────────────────────────────────────


@pytest.mark.asyncio
async def test_place_order_refuses_without_broker_even_if_gate_flipped(
    monkeypatch,
) -> None:
    # Temporarily pretend LIVE_TRADING_ENABLED were True. We do NOT edit
    # the module constant globally — only the imported name in the tool
    # module — to test the "no broker attached" path cleanly.
    from core.tools.builtin import xauusd as xtools
    from core.trading.xauusd.broker import set_broker

    monkeypatch.setattr(xtools, "LIVE_TRADING_ENABLED", True, raising=True)
    set_broker(None)
    out = await xtools.xauusd_place_order_tool.handler(
        {"side": "LONG", "lots": 0.05, "stop_price": 2395.0},
        ToolContext(),
    )
    assert out.is_error
    assert "take_over" in out.content


@pytest.mark.asyncio
async def test_place_order_success_via_mock(monkeypatch) -> None:
    from core.tools.builtin import xauusd as xtools
    from core.trading.xauusd.broker import MockBroker, set_broker

    monkeypatch.setattr(xtools, "LIVE_TRADING_ENABLED", True, raising=True)
    broker = MockBroker()
    set_broker(broker)
    try:
        out = await xtools.xauusd_place_order_tool.handler(
            {
                "side": "LONG",
                "lots": 0.05,
                "order_type": "LIMIT",
                "entry_price": 2400.0,
                "stop_price": 2395.0,
                "take_profit_price": 2410.0,
            },
            ToolContext(),
        )
        assert not out.is_error
        assert out.data["placed"] is True
        assert broker.last_order is not None
        assert broker.last_order.side == "LONG"
        assert broker.last_order.lots == 0.05
    finally:
        set_broker(None)


@pytest.mark.asyncio
async def test_place_order_still_refuses_in_paper_mode() -> None:
    # The default — LIVE_TRADING_ENABLED is False — must refuse even
    # with a broker attached.
    from core.tools.builtin.xauusd import xauusd_place_order_tool
    from core.trading.xauusd.broker import MockBroker, set_broker

    set_broker(MockBroker())
    try:
        out = await xauusd_place_order_tool.handler(
            {"side": "LONG", "lots": 0.05, "stop_price": 2395.0},
            ToolContext(),
        )
        assert out.is_error
        assert "LIVE_TRADING_ENABLED" in out.content
    finally:
        set_broker(None)


# ── flatten_all: calls broker when live, state always flips ───────


@pytest.mark.asyncio
async def test_flatten_all_with_broker_live(monkeypatch) -> None:
    from core.tools.builtin import xauusd as xtools
    from core.trading.xauusd.broker import (
        MockBroker,
        OrderRequest,
        set_broker,
    )

    monkeypatch.setattr(xtools, "LIVE_TRADING_ENABLED", True, raising=True)
    broker = MockBroker()
    # Seed one position so close_all has work to do.
    await broker.place_order(
        OrderRequest(
            side="LONG", lots=0.05, order_type="LIMIT", limit_price=2400.0
        )
    )
    set_broker(broker)
    try:
        # Move out of OFF first so force_disable is a real transition.
        await xtools.xauusd_state_tool.handler(
            {"action": "transition", "to": "SCANNING", "reason": "start"},
            ToolContext(),
        )
        out = await xtools.xauusd_flatten_all_tool.handler(
            {"reason": "test"}, ToolContext()
        )
        assert not out.is_error
        assert out.data["state"] == "DISABLED"
        assert len(out.data["results"]) == 1
    finally:
        set_broker(None)


# ── take_over factory ──────────────────────────────────────────────


class _FakeBrowserSessions:
    """Stand-in for BrowserSessionManager. Exposes ``_pages`` dict."""

    def __init__(self, pages: dict) -> None:
        self._pages = pages


class _FakeHugoswayPage:
    url = "https://trade.hugosway.com/xauusd"

    def get_by_role(self, *a, **kw):  # pragma: no cover - not reached
        raise NotImplementedError("verify_session never reaches this in tests")


@pytest.mark.asyncio
async def test_take_over_requires_confirm_phrase() -> None:
    from core.tools.builtin.xauusd import make_xauusd_take_over_tool

    tool = make_xauusd_take_over_tool(_FakeBrowserSessions({}))
    out = await tool.handler(
        {
            "browser_session_id": "bb-1",
            "account_type": "demo",
            "confirm": "sure",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "TAKEOVER" in out.content


@pytest.mark.asyncio
async def test_take_over_rejects_unknown_account_type() -> None:
    from core.tools.builtin.xauusd import make_xauusd_take_over_tool

    tool = make_xauusd_take_over_tool(_FakeBrowserSessions({}))
    out = await tool.handler(
        {
            "browser_session_id": "bb-1",
            "account_type": "funny-money",
            "confirm": "TAKEOVER",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "demo" in out.content


@pytest.mark.asyncio
async def test_take_over_refuses_unknown_session_id() -> None:
    from core.tools.builtin.xauusd import make_xauusd_take_over_tool

    tool = make_xauusd_take_over_tool(_FakeBrowserSessions({}))
    out = await tool.handler(
        {
            "browser_session_id": "bb-missing",
            "account_type": "demo",
            "confirm": "TAKEOVER",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "no Browserbase page" in out.content


@pytest.mark.asyncio
async def test_get_candles_happy_path(monkeypatch) -> None:
    import json

    import httpx

    from core.tools.builtin.xauusd import xauusd_get_candles_tool

    body = {
        "values": [
            {
                "datetime": "2026-04-19 16:30:00",
                "open": "2400.1",
                "high": "2401.4",
                "low": "2399.9",
                "close": "2401.1",
                "volume": "123",
            }
        ]
    }

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["symbol"] == "XAU/USD"
        assert req.url.params["interval"] == "5min"
        return httpx.Response(200, content=json.dumps(body))

    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)

    # Provide a key via env fallback.
    from core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "twelvedata_api_key", "key-abc", raising=False
    )
    from core.secrets import set_integration_secrets_store

    set_integration_secrets_store(None)

    out = await xauusd_get_candles_tool.handler(
        {"timeframe": "5M", "count": 10}, ToolContext()
    )
    assert not out.is_error
    assert out.data["count"] == 1
    assert out.data["candles"][0]["close"] == pytest.approx(2401.1)
