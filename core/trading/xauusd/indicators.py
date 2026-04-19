"""Deterministic indicator primitives for the XAUUSD rule engine.

All three indicators (EMA, RSI, ADX) are hand-rolled in pure Python so
the engine has zero external dependencies and the tests can pin exact
floating-point outputs. Inputs are always ``list[float]`` (not numpy)
in oldest → newest order; outputs are lists of the same length where
the first few entries may be ``None`` (the warm-up window).

These match the Wilder / standard conventions most trading platforms
use; values have been spot-checked against TA-Lib for XAU/USD 5M fixtures.
"""

from __future__ import annotations


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average with the classic seed = first close.

    The first ``period - 1`` entries are ``None`` (insufficient data);
    the entry at index ``period - 1`` is a simple mean; every entry
    after that follows ``ema_t = alpha * value + (1 - alpha) * ema_prev``
    with ``alpha = 2 / (period + 1)``.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(values)
    out: list[float | None] = [None] * n
    if n < period:
        return out
    alpha = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = alpha * values[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI. Uses smoothed average gain/loss (not simple mean).

    Warm-up: first ``period`` entries are ``None``; entry at index
    ``period`` uses the simple mean of the first ``period`` gains/losses,
    matching Wilder's original spec. Subsequent entries apply the
    smoothing recurrence ``avg_t = ((period - 1) * avg_prev + x_t) / period``.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(values)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    out[period] = _rsi_from_avg(avg_gain, avg_loss)

    for i in range(period + 1, n):
        diff = values[i] - values[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = ((period - 1) * avg_gain + gain) / period
        avg_loss = ((period - 1) * avg_loss + loss) / period
        out[i] = _rsi_from_avg(avg_gain, avg_loss)
    return out


def _rsi_from_avg(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Average Directional Index. Returns ``(+DI, -DI, ADX)`` as lists.

    Implements the standard Wilder algorithm:
        1. Compute directional movement (+DM, -DM) bar-by-bar.
        2. Compute true range (TR).
        3. Smooth each with Wilder's EMA (= simple mean for first
           ``period`` bars, recurrence thereafter).
        4. +DI = 100 * smoothed(+DM) / smoothed(TR)
           -DI = 100 * smoothed(-DM) / smoothed(TR)
        5. DX = 100 * |+DI - -DI| / (+DI + -DI)
        6. ADX = Wilder-smoothed DX.

    Warm-up: indices 0..period-1 are ``None`` for DI; indices
    0..(2*period - 2) are ``None`` for ADX (it needs `period` DX values
    to seed).
    """
    if len(highs) != len(lows) or len(highs) != len(closes):
        raise ValueError("highs/lows/closes must be the same length")
    if period <= 0:
        raise ValueError("period must be positive")

    n = len(highs)
    plus_di: list[float | None] = [None] * n
    minus_di: list[float | None] = [None] * n
    adx_out: list[float | None] = [None] * n
    if n < period + 1:
        return plus_di, minus_di, adx_out

    # Per-bar TR, +DM, -DM starting at index 1 (needs previous bar).
    tr: list[float] = [0.0]
    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        tr.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    # Wilder-smoothed TR, +DM, -DM. Seed = sum of first `period` values
    # (i.e. at index `period`), then smoothed forward.
    tr_s = sum(tr[1 : period + 1])
    pdm_s = sum(plus_dm[1 : period + 1])
    mdm_s = sum(minus_dm[1 : period + 1])
    dx_values: list[float] = []
    if tr_s > 0:
        plus_di[period] = 100.0 * pdm_s / tr_s
        minus_di[period] = 100.0 * mdm_s / tr_s
        dx_values.append(_dx_from_di(plus_di[period], minus_di[period]))
    for i in range(period + 1, n):
        tr_s = tr_s - (tr_s / period) + tr[i]
        pdm_s = pdm_s - (pdm_s / period) + plus_dm[i]
        mdm_s = mdm_s - (mdm_s / period) + minus_dm[i]
        if tr_s > 0:
            pdi = 100.0 * pdm_s / tr_s
            mdi = 100.0 * mdm_s / tr_s
            plus_di[i] = pdi
            minus_di[i] = mdi
            dx_values.append(_dx_from_di(pdi, mdi))

    # ADX = Wilder-smoothed DX. First ADX value sits at index
    # `2*period - 1` (period bars to seed DI + period bars of DX).
    if len(dx_values) >= period:
        adx_prev = sum(dx_values[:period]) / period
        adx_idx = 2 * period - 1
        if adx_idx < n:
            adx_out[adx_idx] = adx_prev
        for j in range(period, len(dx_values)):
            adx_prev = ((period - 1) * adx_prev + dx_values[j]) / period
            idx = period + j
            if idx < n:
                adx_out[idx] = adx_prev
    return plus_di, minus_di, adx_out


def _dx_from_di(pdi: float | None, mdi: float | None) -> float:
    if pdi is None or mdi is None:
        return 0.0
    s = pdi + mdi
    if s == 0:
        return 0.0
    return 100.0 * abs(pdi - mdi) / s


def slope(values: list[float | None], lookback: int) -> float | None:
    """Average-per-candle slope of the last ``lookback`` non-None points.

    Returns the signed change per candle (so *speed* rather than total
    distance). ``None`` if fewer than ``lookback`` points are available
    — the caller decides whether that counts as 'flat' or 'unknown'.
    """
    if lookback <= 1:
        raise ValueError("lookback must be > 1")
    tail = [v for v in values[-lookback:] if v is not None]
    if len(tail) < lookback:
        return None
    return (tail[-1] - tail[0]) / (len(tail) - 1)
