"""Deterministic rule engine for XAU/USD.

One public entrypoint: ``evaluate_setup``. It takes candle series for
every timeframe (5M mandatory; 1M/15M/1H/4H optional when the caller
has them) plus the config, and returns a structured verdict the agent
can act on:

    {
        "verdict": "TAKE_LONG" | "TAKE_SHORT" | "NO_TRADE" | "DISABLED",
        "reason": "one-line summary",
        "details": {
            "mtf":        {...}  # per-TF bias notes
            "structure":  {...}
            "indicators": {...}
            "candle":     {...}
            "regime":     "TRENDING_BULLISH"
        }
    }

The engine never places orders. It never references the broker. It's
a pure function so the tests can pin scenario fixtures and prove the
same inputs always yield the same verdict — that's the whole value.

Ordering of checks matches the spec top-to-bottom. An earlier veto
short-circuits the downstream filters so the ``reason`` field stays
actionable (one reason, most specific one).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.trading.xauusd.candle import Candle, closes, highs, lows
from core.trading.xauusd.config import XAUUSDConfig
from core.trading.xauusd.indicators import adx, ema, rsi, slope
from core.trading.xauusd.structure import (
    Regime,
    StructureLabel,
    classify_regime,
    swing_points,
    trend_structure,
)


class Verdict(str):
    TAKE_LONG = "TAKE_LONG"
    TAKE_SHORT = "TAKE_SHORT"
    NO_TRADE = "NO_TRADE"
    DISABLED = "DISABLED"


@dataclass
class Bias:
    """Per-timeframe snapshot the MTF aligner consumes."""
    direction: str = "NEUTRAL"   # "LONG" | "SHORT" | "NEUTRAL"
    notes: list[str] = field(default_factory=list)


@dataclass
class Evaluation:
    verdict: str
    reason: str
    details: dict[str, Any]


def evaluate_setup(
    *,
    config: XAUUSDConfig,
    candles_5m: list[Candle],
    candles_1m: list[Candle] | None = None,
    candles_15m: list[Candle] | None = None,
    candles_1h: list[Candle] | None = None,
    candles_4h: list[Candle] | None = None,
    spread_usd: float = 0.0,
) -> Evaluation:
    """Run every gate top-to-bottom and return the first blocking one."""

    # ── 0. Sanity + anomaly gate ────────────────────────────
    if not candles_5m or len(candles_5m) < max(
        config.ema_slow_period + 5,
        config.adx_period * 2 + 2,
    ):
        return Evaluation(
            verdict=Verdict.NO_TRADE,
            reason="not enough 5M history for EMA/ADX warm-up",
            details={},
        )
    if spread_usd > config.max_spread_usd:
        return Evaluation(
            verdict=Verdict.NO_TRADE,
            reason=f"spread ${spread_usd:.2f} > cap ${config.max_spread_usd:.2f}",
            details={"spread_usd": spread_usd},
        )

    # ── 1. Indicators on the primary TF ──────────────────────
    c5 = candles_5m
    c5_closes = closes(c5)
    ema_fast = ema(c5_closes, config.ema_fast_period)
    ema_slow = ema(c5_closes, config.ema_slow_period)
    rsi_vals = rsi(c5_closes, config.rsi_period)
    _pdi, _mdi, adx_vals = adx(highs(c5), lows(c5), c5_closes, config.adx_period)

    ema_slope = slope(ema_fast, config.ema_slope_lookback)
    last_close = c5[-1].close
    last_fast = ema_fast[-1]
    last_slow = ema_slow[-1]
    last_rsi = rsi_vals[-1]
    last_adx = adx_vals[-1]

    # ── 2. Structure + regime ───────────────────────────────
    pivots = swing_points(c5, config.swing_lookback)
    structure = trend_structure(pivots)
    regime = classify_regime(
        c5,
        structure,
        last_adx,
        ema_slope,
        config.anomaly_tick_jump_usd,
    )
    if regime.regime is Regime.NEWS_DISTORTED:
        return Evaluation(
            verdict=Verdict.DISABLED,
            reason=f"news-distorted regime: {regime.reason}",
            details={"regime": regime.regime.value},
        )
    if regime.regime in (Regime.CHOP, Regime.RANGE):
        return Evaluation(
            verdict=Verdict.NO_TRADE,
            reason=f"regime {regime.regime.value} — stand aside",
            details={"regime": regime.regime.value, "reason": regime.reason},
        )

    # ── 3. 5M trend filter ──────────────────────────────────
    direction, trend_ok, trend_note = _trend_filter(
        last_close=last_close,
        ema_fast=last_fast,
        ema_slow=last_slow,
        ema_slope_val=ema_slope,
        ema_slope_min_abs=config.ema_slope_min_abs,
        structure=structure,
    )
    if not trend_ok:
        return Evaluation(
            verdict=Verdict.NO_TRADE,
            reason=trend_note,
            details={"structure": structure.value},
        )

    # ── 4. ADX strength filter ──────────────────────────────
    if last_adx is None or last_adx < config.adx_min_trend:
        return Evaluation(
            verdict=Verdict.NO_TRADE,
            reason=(
                f"ADX {last_adx:.1f} below minimum "
                f"{config.adx_min_trend:.1f}"
                if last_adx is not None
                else "ADX warm-up incomplete"
            ),
            details={"adx": last_adx},
        )

    # ── 5. RSI support filter ───────────────────────────────
    if direction == "LONG" and (last_rsi is None or last_rsi < config.rsi_long_support_min):
        return Evaluation(
            verdict=Verdict.NO_TRADE,
            reason=(
                f"RSI {last_rsi:.1f} below long support "
                f"{config.rsi_long_support_min:.1f}"
                if last_rsi is not None
                else "RSI warm-up incomplete"
            ),
            details={"rsi": last_rsi},
        )
    if direction == "SHORT" and (last_rsi is None or last_rsi > config.rsi_short_support_max):
        return Evaluation(
            verdict=Verdict.NO_TRADE,
            reason=(
                f"RSI {last_rsi:.1f} above short support "
                f"{config.rsi_short_support_max:.1f}"
                if last_rsi is not None
                else "RSI warm-up incomplete"
            ),
            details={"rsi": last_rsi},
        )

    # ── 6. Candle-confirmation filter ───────────────────────
    ok, candle_note = _candle_confirmation(c5[-2], c5[-1], direction)
    if not ok:
        return Evaluation(
            verdict=Verdict.NO_TRADE,
            reason=candle_note,
            details={"last_candle": _candle_note(c5[-1])},
        )

    # ── 7. Multi-timeframe alignment ─────────────────────────
    mtf = _mtf_alignment(
        direction=direction,
        config=config,
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        candles_4h=candles_4h,
        candles_1m=candles_1m,
    )
    if config.require_mtf_alignment and not mtf["aligned"] and not config.allow_countertrend:
        return Evaluation(
            verdict=Verdict.NO_TRADE,
            reason=f"MTF misaligned: {mtf['note']}",
            details={"mtf": mtf},
        )

    # ── All gates passed → actionable setup.
    verdict = Verdict.TAKE_LONG if direction == "LONG" else Verdict.TAKE_SHORT
    return Evaluation(
        verdict=verdict,
        reason=f"{direction} setup: {trend_note}",
        details={
            "structure": structure.value,
            "regime": regime.regime.value,
            "regime_reason": regime.reason,
            "indicators": {
                "ema_fast": last_fast,
                "ema_slow": last_slow,
                "ema_slope": ema_slope,
                "rsi": last_rsi,
                "adx": last_adx,
            },
            "mtf": mtf,
            "last_close": last_close,
        },
    )


def _trend_filter(
    *,
    last_close: float,
    ema_fast: float | None,
    ema_slow: float | None,
    ema_slope_val: float | None,
    ema_slope_min_abs: float,
    structure: StructureLabel,
) -> tuple[str, bool, str]:
    """Return (direction, ok, note)."""
    if ema_fast is None or ema_slow is None:
        return ("NEUTRAL", False, "EMA warm-up incomplete")
    if ema_slope_val is None:
        return ("NEUTRAL", False, "EMA slope unknown (not enough history)")
    if abs(ema_slope_val) < ema_slope_min_abs:
        return (
            "NEUTRAL",
            False,
            f"EMA slope {ema_slope_val:.3f} below floor {ema_slope_min_abs:.3f}",
        )
    bullish_stack = last_close > ema_fast > ema_slow and ema_slope_val > 0
    bearish_stack = last_close < ema_fast < ema_slow and ema_slope_val < 0
    if bullish_stack and structure is not StructureLabel.LL_LH:
        return ("LONG", True, "price > fastEMA > slowEMA, slope+, HH/HL")
    if bearish_stack and structure is not StructureLabel.HH_HL:
        return ("SHORT", True, "price < fastEMA < slowEMA, slope-, LL/LH")
    return ("NEUTRAL", False, "price/EMA stack not cleanly bullish or bearish")


def _candle_confirmation(prev: Candle, last: Candle, direction: str) -> tuple[bool, str]:
    """Accept engulfings + strong-body continuations; reject indecision."""
    body = abs(last.body)
    if last.range <= 0:
        return False, "flat candle (range == 0)"
    body_ratio = body / last.range
    if body_ratio < 0.5:
        return False, f"weak-body last candle (body/range={body_ratio:.2f})"
    if direction == "LONG":
        if not last.is_bullish:
            return False, "last candle is not bullish"
        engulf = last.close > prev.open and last.open <= prev.close
        strong_impulse = body >= prev.range * 0.6
        if engulf or strong_impulse:
            return True, "bullish engulfing or strong-body continuation"
        return False, "no bullish engulfing / strong impulse"
    if direction == "SHORT":
        if not last.is_bearish:
            return False, "last candle is not bearish"
        engulf = last.close < prev.open and last.open >= prev.close
        strong_impulse = body >= prev.range * 0.6
        if engulf or strong_impulse:
            return True, "bearish engulfing or strong-body continuation"
        return False, "no bearish engulfing / strong impulse"
    return False, "unknown direction"


def _mtf_alignment(
    *,
    direction: str,
    config: XAUUSDConfig,
    candles_15m: list[Candle] | None,
    candles_1h: list[Candle] | None,
    candles_4h: list[Candle] | None,
    candles_1m: list[Candle] | None,
) -> dict[str, Any]:
    per_tf = {
        "15m": _bias_for(candles_15m, config),
        "1h": _bias_for(candles_1h, config),
        "4h": _bias_for(candles_4h, config),
        "1m": _bias_for(candles_1m, config),
    }
    # 1M by itself never creates a bias; it's refinement-only per spec.
    structural = [per_tf["15m"], per_tf["1h"], per_tf["4h"]]
    supportive = [
        b.direction == direction or b.direction == "NEUTRAL" for b in structural
    ]
    aligned = all(supportive)
    note = ", ".join(
        f"{tf}={bias.direction}" for tf, bias in per_tf.items()
    )
    return {
        "aligned": aligned,
        "note": note,
        "per_tf": {tf: {"direction": b.direction, "notes": b.notes} for tf, b in per_tf.items()},
    }


def _bias_for(candles: list[Candle] | None, config: XAUUSDConfig) -> Bias:
    if not candles or len(candles) < config.ema_fast_period + 3:
        return Bias(direction="NEUTRAL", notes=["insufficient history"])
    fast = ema(closes(candles), config.ema_fast_period)
    s = slope(fast, min(config.ema_slope_lookback, len(candles) - 1))
    last_close = candles[-1].close
    last_fast = fast[-1]
    if last_fast is None or s is None:
        return Bias(direction="NEUTRAL", notes=["warm-up incomplete"])
    if last_close > last_fast and s > 0:
        return Bias(
            direction="LONG",
            notes=[f"close>{config.ema_fast_period}EMA, slope+"],
        )
    if last_close < last_fast and s < 0:
        return Bias(
            direction="SHORT",
            notes=[f"close<{config.ema_fast_period}EMA, slope-"],
        )
    return Bias(direction="NEUTRAL", notes=["mixed EMA/slope"])


def _candle_note(c: Candle) -> dict[str, Any]:
    return {
        "o": c.open,
        "h": c.high,
        "l": c.low,
        "c": c.close,
        "bull": c.is_bullish,
        "body": c.body,
        "range": c.range,
    }


# Re-export for ergonomic ``from .rules import ...``.
__all__ = ["Bias", "Evaluation", "Verdict", "evaluate_setup"]
