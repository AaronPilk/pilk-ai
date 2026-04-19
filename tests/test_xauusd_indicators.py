"""Golden-value tests for the hand-rolled indicator primitives.

Each test pins an expected numeric output against a hand-curated input
so a regression shows up loud. Values were computed with a separate
scratch script and cross-checked against Wilder's original definitions.
"""

from __future__ import annotations

import math

import pytest

from core.trading.xauusd.indicators import adx, ema, rsi, slope


def _approx(a: float | None, b: float, tol: float = 1e-6) -> bool:
    if a is None:
        return False
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


# ── EMA ───────────────────────────────────────────────────────────


def test_ema_period_one_is_identity() -> None:
    # With period=1, alpha=1, so EMA_t == value_t.
    values = [1.0, 2.0, 3.0, 4.0]
    out = ema(values, 1)
    assert out == values


def test_ema_warmup_returns_none() -> None:
    out = ema([1.0, 2.0], 5)
    assert out == [None, None]


def test_ema_seed_is_sma_of_first_period() -> None:
    # period=3 → seed at index 2 = mean([1,2,3]) = 2.0
    values = [1.0, 2.0, 3.0, 4.0]
    out = ema(values, 3)
    assert out[0] is None
    assert out[1] is None
    assert _approx(out[2], 2.0)
    # index 3: alpha=2/(3+1)=0.5; new = 0.5*4 + 0.5*2 = 3.0
    assert _approx(out[3], 3.0)


def test_ema_converges_on_flat_input() -> None:
    # Constant series → EMA stays at the constant once warmed up.
    values = [5.0] * 20
    out = ema(values, 5)
    assert _approx(out[-1], 5.0)


# ── RSI ────────────────────────────────────────────────────────────


def test_rsi_all_gains_is_100() -> None:
    # Strict monotonic increase → avg_loss=0 → RSI=100.
    values = [float(i) for i in range(1, 20)]
    out = rsi(values, 14)
    assert _approx(out[-1], 100.0)


def test_rsi_all_losses_is_0() -> None:
    values = [float(i) for i in range(20, 0, -1)]
    out = rsi(values, 14)
    assert _approx(out[-1], 0.0)


def test_rsi_neutral_on_flat_input() -> None:
    # No movement → both avg_gain and avg_loss are zero. By
    # convention our implementation returns 100 when avg_loss is 0 —
    # document + lock that behavior here.
    values = [100.0] * 20
    out = rsi(values, 14)
    assert _approx(out[-1], 100.0)


def test_rsi_warmup_returns_none() -> None:
    out = rsi([1.0, 2.0, 3.0], 14)
    assert all(v is None for v in out)


# ── ADX ───────────────────────────────────────────────────────────


def test_adx_flat_market_is_zero() -> None:
    # All candles identical → no directional movement, no TR → +DI,
    # -DI, ADX all stay None after warm-up because we guard
    # division-by-zero. Locking that behavior here.
    n = 40
    highs = [100.0] * n
    lows = [99.0] * n
    closes = [99.5] * n
    pdi, mdi, adx_vals = adx(highs, lows, closes, 14)
    # With flat highs/lows, TR is also effectively constant (hi-lo=1)
    # so +DI and -DI will be 0 once smoothed. ADX should end up 0 too.
    assert adx_vals[-1] is not None
    assert _approx(adx_vals[-1], 0.0, tol=1e-6)
    assert _approx(pdi[-1], 0.0, tol=1e-6)
    assert _approx(mdi[-1], 0.0, tol=1e-6)


def test_adx_trending_up_has_positive_di_dominance() -> None:
    # Clean uptrend: every candle higher than the last.
    n = 40
    highs = [100.0 + i for i in range(n)]
    lows = [99.0 + i for i in range(n)]
    closes = [99.5 + i for i in range(n)]
    pdi, mdi, adx_vals = adx(highs, lows, closes, 14)
    assert pdi[-1] is not None and mdi[-1] is not None
    assert pdi[-1] > mdi[-1]
    # ADX should be meaningfully above zero in a pure trend.
    assert adx_vals[-1] is not None
    assert adx_vals[-1] > 10.0


def test_adx_warmup_is_none() -> None:
    n = 10
    pdi, mdi, adx_vals = adx([1.0] * n, [0.5] * n, [0.75] * n, 14)
    assert all(v is None for v in pdi)
    assert all(v is None for v in mdi)
    assert all(v is None for v in adx_vals)


# ── slope ─────────────────────────────────────────────────────────


def test_slope_simple_linear() -> None:
    values: list[float | None] = [1.0, 2.0, 3.0, 4.0, 5.0]
    # Average change per candle over last 5 values = 1.0
    assert _approx(slope(values, 5), 1.0)


def test_slope_handles_leading_none() -> None:
    values: list[float | None] = [None, None, 10.0, 11.0, 12.0]
    # Only 3 non-None — lookback=5 expects 5, so returns None.
    assert slope(values, 5) is None
    # lookback=3 → tail has 3 non-None → (12 - 10) / 2 = 1.0
    assert _approx(slope(values, 3), 1.0)


def test_slope_requires_lookback_gt_one() -> None:
    with pytest.raises(ValueError, match="lookback"):
        slope([1.0, 2.0], 1)
