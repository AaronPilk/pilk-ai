"""Twelve Data price-feed adapter for XAU/USD.

Thin ``httpx`` wrapper around Twelve Data's ``/time_series`` endpoint.
Returns ``Candle`` objects in the shape the rule engine already expects,
so the tool layer can swap raw payloads for live bars with zero other
changes.

Free tier (8 req/min, 800 req/day) is the realistic ceiling for a single
operator. The agent's cadence spec — 5M candle on every new bar, HTF
refresh on its own cadence — stays well under that budget.

Not a WebSocket feed: the agent pulls candles on demand, not tick by
tick. This is a deliberate match for the paper-first design: every
evaluation is repeatable from the stored history, and the first missed
tick doesn't silently flip a trade.

Injectable ``httpx`` client so tests can hand in a ``MockTransport``
without touching the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from core.trading.xauusd.candle import Candle

TWELVEDATA_BASE = "https://api.twelvedata.com"
DEFAULT_TIMEOUT_S = 15.0

# PILK timeframe labels → Twelve Data interval strings. The mapping is
# deliberate and exhaustive: the rule engine only reasons in these five.
TIMEFRAME_INTERVAL: dict[str, str] = {
    "1M": "1min",
    "5M": "5min",
    "15M": "15min",
    "1H": "1h",
    "4H": "4h",
}

# Twelve Data's symbol string for spot gold. Their docs also accept
# "XAUUSD" but "XAU/USD" is the canonical one shown on their dashboard.
TWELVEDATA_SYMBOL = "XAU/USD"


class FeedError(Exception):
    """Raised for any non-recoverable feed failure (auth, shape, network).

    The tool layer catches this and returns a friendly ``is_error``
    outcome so the agent can route to ``NO_TRADE`` rather than crash.
    """


@dataclass(frozen=True)
class FetchResult:
    """Shape returned by ``TwelveDataFeed.fetch_candles``.

    Candles come oldest-first so callers can treat them as a rolling
    window without reversing. ``fetched_at`` is the Twelve Data
    server-reported timestamp if present — otherwise None.
    """

    timeframe: str
    candles: list[Candle]
    fetched_at: str | None


class TwelveDataFeed:
    """Single-responsibility client for XAU/USD candles."""

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = TWELVEDATA_BASE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not api_key:
            # Fail loud at construction so we never send a request with
            # an empty ``apikey`` param (Twelve Data 401s and burns quota).
            raise FeedError("twelve_data_api_key is empty")
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._base_url = base_url.rstrip("/")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_candles(
        self,
        timeframe: str,
        count: int = 200,
        *,
        symbol: str = TWELVEDATA_SYMBOL,
    ) -> FetchResult:
        interval = TIMEFRAME_INTERVAL.get(timeframe.upper())
        if interval is None:
            raise FeedError(
                f"unsupported timeframe '{timeframe}'. "
                f"Known: {sorted(TIMEFRAME_INTERVAL)}"
            )
        # Twelve Data caps outputsize at 5000 on paid tiers and 8 on the
        # default free sample; 500 is a comfortable ceiling for any
        # realistic XAU/USD analysis.
        outputsize = max(1, min(int(count), 500))
        params = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": self._api_key,
            # Oldest first — matches how the rule engine reads bars.
            "order": "ASC",
            "format": "JSON",
        }
        try:
            resp = await self._client.get(
                f"{self._base_url}/time_series", params=params
            )
        except httpx.HTTPError as e:
            raise FeedError(f"feed transport error: {e}") from e

        if resp.status_code >= 500:
            raise FeedError(
                f"feed upstream {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code == 401:
            raise FeedError("twelve_data: unauthorized (bad or missing key)")
        if resp.status_code == 429:
            raise FeedError(
                "twelve_data: rate limited — free tier is 8 req/min, "
                "800/day. Slow the cadence or upgrade the plan."
            )
        if resp.status_code >= 400:
            raise FeedError(
                f"feed client error {resp.status_code}: {resp.text[:200]}"
            )

        try:
            payload: dict[str, Any] = resp.json()
        except ValueError as e:
            raise FeedError(f"feed returned non-JSON: {e}") from e

        # Twelve Data reports API-level errors inside a 200 body with
        # ``status: "error"``. Treat that exactly like an HTTP failure.
        if payload.get("status") == "error":
            raise FeedError(
                f"twelve_data error: {payload.get('message', 'unknown')}"
            )

        values = payload.get("values")
        if not isinstance(values, list) or not values:
            raise FeedError(
                "twelve_data returned no candles — check symbol, interval, "
                "or whether the market is open."
            )

        candles: list[Candle] = []
        for i, row in enumerate(values):
            try:
                candles.append(_row_to_candle(row, index=i))
            except (KeyError, TypeError, ValueError) as e:
                raise FeedError(f"candle row {i} malformed: {e}") from e
        return FetchResult(
            timeframe=timeframe.upper(),
            candles=candles,
            fetched_at=str(payload.get("meta", {}).get("updated_at") or "")
            or None,
        )


def _row_to_candle(row: dict[str, Any], *, index: int) -> Candle:
    """Coerce one Twelve Data row into a ``Candle``.

    Their payload uses ISO-ish ``datetime`` strings; we store the
    epoch-second integer the rule engine already treats as its
    monotonic-ordering key. Falling back to ``index`` keeps a total
    ordering even if a row is missing the datetime key.
    """
    ts_raw = row.get("datetime") or row.get("timestamp")
    ts = _parse_ts(ts_raw) if ts_raw is not None else index
    return Candle(
        ts=int(ts),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row.get("volume") or 0.0),
    )


def _parse_ts(value: Any) -> int:
    """Best-effort epoch-second conversion.

    Accepts:
        * already-epoch ints ("1713542400" / 1713542400)
        * "YYYY-MM-DD HH:MM:SS" (Twelve Data default)
        * "YYYY-MM-DD" (daily bars)
    """
    if isinstance(value, int | float):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    from datetime import UTC, datetime

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=UTC)
            return int(dt.timestamp())
        except ValueError:
            continue
    raise ValueError(f"unparseable datetime: {value!r}")


__all__ = [
    "TIMEFRAME_INTERVAL",
    "TWELVEDATA_BASE",
    "TWELVEDATA_SYMBOL",
    "FeedError",
    "FetchResult",
    "TwelveDataFeed",
]
