"""Scenario-based tests for the XAU/USD rule engine.

Synthetic candle series drive the engine through each gate. Not a
backtest — the goal is pinned behavior: same input, same verdict, same
reason substring.
"""

from __future__ import annotations

import pytest

from core.trading.xauusd.candle import Candle
from core.trading.xauusd.config import XAUUSDConfig
from core.trading.xauusd.rules import Verdict, evaluate_setup


def _uptrend(n: int, start: float = 2400.0, step: float = 0.80) -> list[Candle]:
    """A synthetic steady uptrend. Each bar is a clean bullish candle
    with a small upper wick. Good enough to pass EMA + ADX filters."""
    bars: list[Candle] = []
    price = start
    for i in range(n):
        o = price
        c = price + step
        h = c + 0.2
        lo = o - 0.15
        bars.append(Candle(ts=i * 300, open=o, high=h, low=lo, close=c))
        price = c
    return bars


def _downtrend(n: int, start: float = 2400.0, step: float = 0.80) -> list[Candle]:
    bars: list[Candle] = []
    price = start
    for i in range(n):
        o = price
        c = price - step
        h = o + 0.15
        lo = c - 0.2
        bars.append(Candle(ts=i * 300, open=o, high=h, low=lo, close=c))
        price = c
    return bars


def _flat(n: int, price: float = 2400.0) -> list[Candle]:
    return [
        Candle(ts=i * 300, open=price, high=price + 0.1, low=price - 0.1, close=price)
        for i in range(n)
    ]


def test_insufficient_history_returns_no_trade() -> None:
    cfg = XAUUSDConfig()
    ev = evaluate_setup(config=cfg, candles_5m=_uptrend(20))
    assert ev.verdict == Verdict.NO_TRADE
    assert "warm-up" in ev.reason


def test_wide_spread_blocks_trade() -> None:
    cfg = XAUUSDConfig()
    ev = evaluate_setup(
        config=cfg,
        candles_5m=_uptrend(250),
        spread_usd=2.00,  # > 0.50 cap
    )
    assert ev.verdict == Verdict.NO_TRADE
    assert "spread" in ev.reason


def test_flat_tape_classified_and_blocked() -> None:
    cfg = XAUUSDConfig()
    ev = evaluate_setup(config=cfg, candles_5m=_flat(250))
    # Flat tape → range/chop → NO_TRADE.
    assert ev.verdict == Verdict.NO_TRADE
    assert ev.reason.startswith("regime ") or "regime" in ev.reason.lower()


def test_clean_uptrend_takes_long() -> None:
    cfg = XAUUSDConfig()
    # 15M/1H/4H all supportive uptrends so MTF alignment passes.
    ev = evaluate_setup(
        config=cfg,
        candles_5m=_uptrend(250),
        candles_15m=_uptrend(80),
        candles_1h=_uptrend(80),
        candles_4h=_uptrend(80),
        spread_usd=0.20,
    )
    assert ev.verdict == Verdict.TAKE_LONG
    assert "LONG" in ev.reason
    assert ev.details["mtf"]["aligned"] is True


def test_clean_downtrend_takes_short() -> None:
    cfg = XAUUSDConfig()
    ev = evaluate_setup(
        config=cfg,
        candles_5m=_downtrend(250),
        candles_15m=_downtrend(80),
        candles_1h=_downtrend(80),
        candles_4h=_downtrend(80),
        spread_usd=0.20,
    )
    assert ev.verdict == Verdict.TAKE_SHORT


def test_mtf_misalignment_blocks_trade_when_required() -> None:
    cfg = XAUUSDConfig(require_mtf_alignment=True, allow_countertrend=False)
    # 5M bullish, HTFs bearish.
    ev = evaluate_setup(
        config=cfg,
        candles_5m=_uptrend(250),
        candles_15m=_downtrend(80),
        candles_1h=_downtrend(80),
        candles_4h=_downtrend(80),
        spread_usd=0.20,
    )
    assert ev.verdict == Verdict.NO_TRADE
    assert "MTF" in ev.reason


def test_countertrend_allowed_when_configured() -> None:
    cfg = XAUUSDConfig(
        require_mtf_alignment=True,
        allow_countertrend=True,
    )
    # 5M bullish against HTFs — with countertrend allowed, goes through.
    ev = evaluate_setup(
        config=cfg,
        candles_5m=_uptrend(250),
        candles_15m=_downtrend(80),
        candles_1h=_downtrend(80),
        candles_4h=_downtrend(80),
        spread_usd=0.20,
    )
    assert ev.verdict == Verdict.TAKE_LONG


def test_news_distortion_disables_engine() -> None:
    # Inject a single giant-range candle near the end of a trend.
    cfg = XAUUSDConfig(anomaly_tick_jump_usd=5.0)
    candles = _uptrend(250)
    spike = candles[-3]
    # Replace with a single $10 candle (range > 5).
    candles[-3] = Candle(
        ts=spike.ts,
        open=spike.open,
        high=spike.open + 8.0,
        low=spike.open - 2.0,
        close=spike.close,
    )
    ev = evaluate_setup(
        config=cfg,
        candles_5m=candles,
        candles_15m=_uptrend(80),
        candles_1h=_uptrend(80),
        candles_4h=_uptrend(80),
        spread_usd=0.20,
    )
    assert ev.verdict == Verdict.DISABLED
    assert "news" in ev.reason.lower()


def test_deterministic_same_inputs_same_verdict() -> None:
    cfg = XAUUSDConfig()
    inputs = dict(
        candles_5m=_uptrend(250),
        candles_15m=_uptrend(80),
        candles_1h=_uptrend(80),
        candles_4h=_uptrend(80),
        spread_usd=0.20,
    )
    a = evaluate_setup(config=cfg, **inputs)
    b = evaluate_setup(config=cfg, **inputs)
    assert a.verdict == b.verdict
    assert a.reason == b.reason


@pytest.mark.parametrize(
    "candles",
    [
        _uptrend(250),
        _downtrend(250),
        _flat(250),
    ],
)
def test_never_crashes_on_legal_shape(candles: list[Candle]) -> None:
    cfg = XAUUSDConfig()
    ev = evaluate_setup(config=cfg, candles_5m=candles, spread_usd=0.20)
    # Any of the four verdicts, but always a string + non-empty reason.
    assert ev.verdict in {
        Verdict.TAKE_LONG,
        Verdict.TAKE_SHORT,
        Verdict.NO_TRADE,
        Verdict.DISABLED,
    }
    assert ev.reason
