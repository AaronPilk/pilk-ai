"""Twelve Data price-feed adapter tests.

All network calls are stubbed via ``httpx.MockTransport``. Covers:

- Constructor refuses an empty API key.
- Unknown timeframe raises ``FeedError`` before any request.
- Happy-path parses ``values`` into oldest-first ``Candle`` objects.
- Timeframe → interval mapping sends the correct query string.
- Twelve Data JSON error (``status: "error"``) surfaces as ``FeedError``.
- HTTP 401 / 429 / 5xx surface as ``FeedError`` with actionable text.
- Malformed rows surface the row index in the error message.
- ISO timestamp parser handles both "YYYY-MM-DD HH:MM:SS" and epoch.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from core.trading.xauusd.feed import (
    TIMEFRAME_INTERVAL,
    FeedError,
    TwelveDataFeed,
    _parse_ts,
)


def _feed_with(handler: Callable[[httpx.Request], httpx.Response]) -> TwelveDataFeed:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TwelveDataFeed("key-abc", client=client)


def test_empty_key_raises() -> None:
    with pytest.raises(FeedError, match="empty"):
        TwelveDataFeed("")


@pytest.mark.asyncio
async def test_unknown_timeframe_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        # Assertion: unknown tf must fail before any request.
        raise AssertionError("must not hit the network")

    feed = _feed_with(handler)
    with pytest.raises(FeedError, match="unsupported timeframe"):
        await feed.fetch_candles("30s")
    await feed.aclose()


@pytest.mark.asyncio
async def test_happy_path_oldest_first() -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(req.url.params)
        body = {
            "meta": {"updated_at": "2026-04-19T16:45:00Z"},
            "values": [
                {
                    "datetime": "2026-04-19 16:30:00",
                    "open": "2400.10",
                    "high": "2401.40",
                    "low": "2399.90",
                    "close": "2401.10",
                    "volume": "123",
                },
                {
                    "datetime": "2026-04-19 16:35:00",
                    "open": "2401.10",
                    "high": "2402.20",
                    "low": "2400.80",
                    "close": "2402.00",
                    "volume": "110",
                },
            ],
            "status": "ok",
        }
        return httpx.Response(200, content=json.dumps(body))

    feed = _feed_with(handler)
    result = await feed.fetch_candles("5M", count=50)
    await feed.aclose()

    assert result.timeframe == "5M"
    assert len(result.candles) == 2
    assert captured["symbol"] == "XAU/USD"
    assert captured["interval"] == "5min"
    assert captured["outputsize"] == "50"
    assert captured["order"] == "ASC"
    assert result.candles[0].close == pytest.approx(2401.10)
    assert result.candles[1].close == pytest.approx(2402.00)
    assert result.fetched_at == "2026-04-19T16:45:00Z"


@pytest.mark.asyncio
async def test_all_intervals_map_correctly() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params["interval"])
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "values": [
                        {
                            "datetime": "2026-04-19 00:00:00",
                            "open": "1",
                            "high": "1",
                            "low": "1",
                            "close": "1",
                        }
                    ]
                }
            ),
        )

    feed = _feed_with(handler)
    for tf in TIMEFRAME_INTERVAL:
        await feed.fetch_candles(tf, count=1)
    await feed.aclose()

    assert seen == ["1min", "5min", "15min", "1h", "4h"]


@pytest.mark.asyncio
async def test_outputsize_clamped_to_500() -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(req.url.params)
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "values": [
                        {
                            "datetime": "2026-04-19 00:00:00",
                            "open": "1",
                            "high": "1",
                            "low": "1",
                            "close": "1",
                        }
                    ]
                }
            ),
        )

    feed = _feed_with(handler)
    await feed.fetch_candles("1M", count=50_000)
    await feed.aclose()
    assert captured["outputsize"] == "500"


@pytest.mark.asyncio
async def test_twelvedata_error_body() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "status": "error",
                    "message": "symbol not subscribed on this plan",
                }
            ),
        )

    feed = _feed_with(handler)
    with pytest.raises(FeedError, match="symbol not subscribed"):
        await feed.fetch_candles("5M")
    await feed.aclose()


@pytest.mark.asyncio
async def test_401_surfaces_auth_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content="unauth")

    feed = _feed_with(handler)
    with pytest.raises(FeedError, match="unauthorized"):
        await feed.fetch_candles("5M")
    await feed.aclose()


@pytest.mark.asyncio
async def test_429_surfaces_rate_limit() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content="slow down")

    feed = _feed_with(handler)
    with pytest.raises(FeedError, match="rate limited"):
        await feed.fetch_candles("5M")
    await feed.aclose()


@pytest.mark.asyncio
async def test_500_surfaces_upstream() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content="bad gateway")

    feed = _feed_with(handler)
    with pytest.raises(FeedError, match="upstream 502"):
        await feed.fetch_candles("5M")
    await feed.aclose()


@pytest.mark.asyncio
async def test_empty_values_surfaces() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"values": []}))

    feed = _feed_with(handler)
    with pytest.raises(FeedError, match="no candles"):
        await feed.fetch_candles("5M")
    await feed.aclose()


@pytest.mark.asyncio
async def test_malformed_row_reports_index() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "values": [
                        {
                            "datetime": "2026-04-19 00:00:00",
                            "open": "1",
                            "high": "1",
                            "low": "1",
                            "close": "1",
                        },
                        # Missing "close".
                        {
                            "datetime": "2026-04-19 00:05:00",
                            "open": "1",
                            "high": "1",
                            "low": "1",
                        },
                    ]
                }
            ),
        )

    feed = _feed_with(handler)
    with pytest.raises(FeedError, match="row 1 malformed"):
        await feed.fetch_candles("5M")
    await feed.aclose()


# ── timestamp parser ──────────────────────────────────────────────

def test_parse_ts_epoch_int() -> None:
    assert _parse_ts(1713542400) == 1713542400


def test_parse_ts_epoch_string() -> None:
    assert _parse_ts("1713542400") == 1713542400


def test_parse_ts_datetime_space() -> None:
    # 2026-04-19 16:30:00 UTC
    assert _parse_ts("2026-04-19 16:30:00") > 0


def test_parse_ts_date_only() -> None:
    assert _parse_ts("2026-04-19") > 0


def test_parse_ts_garbage_raises() -> None:
    with pytest.raises(ValueError):
        _parse_ts("not-a-date")
