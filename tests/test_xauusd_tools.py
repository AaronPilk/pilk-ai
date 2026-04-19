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
