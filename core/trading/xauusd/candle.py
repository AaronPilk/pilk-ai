"""Candle model + helpers shared by every module in this package.

Everything downstream (indicators, structure detection, rule engine)
accepts ``list[Candle]`` sorted oldest → newest. No pandas; the input
shapes are small enough that vanilla Python keeps the tests fast and
the dependency graph tight.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candle:
    ts: int          # unix seconds (UTC) at open
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def body(self) -> float:
        return self.close - self.open

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low


def last_n(candles: list[Candle], n: int) -> list[Candle]:
    if n <= 0 or n > len(candles):
        return list(candles)
    return candles[-n:]


def closes(candles: list[Candle]) -> list[float]:
    return [c.close for c in candles]


def highs(candles: list[Candle]) -> list[float]:
    return [c.high for c in candles]


def lows(candles: list[Candle]) -> list[float]:
    return [c.low for c in candles]
