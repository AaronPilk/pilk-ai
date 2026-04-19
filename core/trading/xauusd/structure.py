"""Market structure detection for the XAUUSD rule engine.

Three responsibilities:

1. ``swing_points`` — find confirmed pivot highs/lows on a candle
   series using an N-bar symmetric lookback. A pivot high is a candle
   whose high is strictly greater than the high of the ``lookback``
   candles on each side; pivot low is the symmetric version.

2. ``trend_structure`` — given the last few pivots, label the sequence
   as ``HH_HL`` (bullish), ``LL_LH`` (bearish), or ``MIXED`` / ``NONE``.

3. ``classify_regime`` — map indicators + structure to one of the
   regime labels the rule engine consumes (``TRENDING_BULLISH``,
   ``TRENDING_BEARISH``, ``PULLBACK``, ``BREAKOUT_FORMING``,
   ``REVERSAL_FORMING``, ``RANGE``, ``CHOP``, ``NEWS_DISTORTED``).

These are pure functions; no state, no side effects, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from core.trading.xauusd.candle import Candle


class PivotKind(StrEnum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class Pivot:
    index: int
    price: float
    kind: PivotKind


def swing_points(candles: list[Candle], lookback: int) -> list[Pivot]:
    """Return confirmed pivots in chronological order.

    A candle at index ``i`` is a pivot high iff its ``high`` is strictly
    greater than ``highs[i-k]`` and ``highs[i+k]`` for every ``k`` in
    ``1..lookback``. Pivot lows are symmetric. Candles within
    ``lookback`` of either end can't be confirmed and are skipped.
    """
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    n = len(candles)
    pivots: list[Pivot] = []
    for i in range(lookback, n - lookback):
        this_high = candles[i].high
        this_low = candles[i].low
        is_high = all(
            this_high > candles[i + k].high and this_high > candles[i - k].high
            for k in range(1, lookback + 1)
        )
        is_low = all(
            this_low < candles[i + k].low and this_low < candles[i - k].low
            for k in range(1, lookback + 1)
        )
        if is_high:
            pivots.append(Pivot(i, this_high, PivotKind.HIGH))
        elif is_low:
            pivots.append(Pivot(i, this_low, PivotKind.LOW))
    return pivots


class StructureLabel(StrEnum):
    HH_HL = "HH_HL"   # higher highs + higher lows → bullish
    LL_LH = "LL_LH"   # lower lows + lower highs → bearish
    MIXED = "MIXED"   # one but not the other
    NONE = "NONE"     # not enough pivots


def trend_structure(pivots: list[Pivot]) -> StructureLabel:
    """Label the last four pivots as bullish / bearish / mixed.

    Four pivots (2 highs + 2 lows) is the minimum that lets us check
    whether *both* the highs and lows are marching in the same
    direction. We walk backward and grab the two most recent of each
    kind; if the newer is above the older for both, structure is bullish.
    """
    last_highs: list[Pivot] = []
    last_lows: list[Pivot] = []
    for p in reversed(pivots):
        if p.kind is PivotKind.HIGH and len(last_highs) < 2:
            last_highs.append(p)
        elif p.kind is PivotKind.LOW and len(last_lows) < 2:
            last_lows.append(p)
        if len(last_highs) == 2 and len(last_lows) == 2:
            break
    if len(last_highs) < 2 or len(last_lows) < 2:
        return StructureLabel.NONE
    # reversed(): last_*[0] is newer, last_*[1] is older.
    higher_highs = last_highs[0].price > last_highs[1].price
    higher_lows = last_lows[0].price > last_lows[1].price
    lower_highs = last_highs[0].price < last_highs[1].price
    lower_lows = last_lows[0].price < last_lows[1].price
    if higher_highs and higher_lows:
        return StructureLabel.HH_HL
    if lower_highs and lower_lows:
        return StructureLabel.LL_LH
    return StructureLabel.MIXED


class Regime(StrEnum):
    TRENDING_BULLISH = "TRENDING_BULLISH"
    TRENDING_BEARISH = "TRENDING_BEARISH"
    PULLBACK = "PULLBACK"
    BREAKOUT_FORMING = "BREAKOUT_FORMING"
    REVERSAL_FORMING = "REVERSAL_FORMING"
    RANGE = "RANGE"
    CHOP = "CHOP"
    NEWS_DISTORTED = "NEWS_DISTORTED"


@dataclass(frozen=True)
class RegimeSnapshot:
    regime: Regime
    reason: str


def classify_regime(
    candles: list[Candle],
    structure: StructureLabel,
    adx_value: float | None,
    ema_slope: float | None,
    anomaly_tick_jump_usd: float,
) -> RegimeSnapshot:
    """Single-pass regime label the rule engine consumes.

    Priority (high → low):
    1. NEWS_DISTORTED if a single candle's range exceeds
       ``anomaly_tick_jump_usd`` (protects against spikes).
    2. TRENDING_{BULL,BEAR} if structure agrees + ADX shows real trend.
    3. PULLBACK if structure is trending but the last few candles
       retrace against it.
    4. RANGE if structure is NONE / MIXED and ADX is weak.
    5. CHOP otherwise.
    """
    if not candles:
        return RegimeSnapshot(Regime.CHOP, "empty candle series")

    # (1) Anomaly first — a single $12+ candle on 5M XAU/USD is almost
    # always news-driven and the rule engine should back off.
    recent = candles[-5:]
    if any(c.range >= anomaly_tick_jump_usd for c in recent):
        return RegimeSnapshot(
            Regime.NEWS_DISTORTED,
            f"single-candle range >= ${anomaly_tick_jump_usd}",
        )

    strong_adx = (adx_value or 0.0) >= 20.0
    slope_up = (ema_slope or 0.0) > 0
    slope_down = (ema_slope or 0.0) < 0

    # (2) Trending: structure confirms OR — for clean impulses where no
    # pivots have printed yet — ADX + slope alone carry the signal. A
    # monotonic uptrend has no swing highs (every high is the highest),
    # so demanding ``HH_HL`` would perversely lock the engine out of
    # the strongest trends.
    if strong_adx and slope_up and structure is not StructureLabel.LL_LH:
        return RegimeSnapshot(
            Regime.TRENDING_BULLISH,
            f"ADX>=20 + EMA slope up (structure={structure.value})",
        )
    if strong_adx and slope_down and structure is not StructureLabel.HH_HL:
        return RegimeSnapshot(
            Regime.TRENDING_BEARISH,
            f"ADX>=20 + EMA slope down (structure={structure.value})",
        )

    # (3) Pullback: trending structure but recent candles retrace.
    last = candles[-1]
    if structure is StructureLabel.HH_HL and last.is_bearish:
        return RegimeSnapshot(Regime.PULLBACK, "HH/HL with recent bearish retrace")
    if structure is StructureLabel.LL_LH and last.is_bullish:
        return RegimeSnapshot(Regime.PULLBACK, "LL/LH with recent bullish retrace")

    if structure in (StructureLabel.NONE, StructureLabel.MIXED) and not strong_adx:
        return RegimeSnapshot(Regime.RANGE, "no clear pivots + weak ADX")
    return RegimeSnapshot(Regime.CHOP, "default: no regime conditions matched")
