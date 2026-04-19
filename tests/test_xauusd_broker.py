"""Broker layer tests — covers MockBroker + the broker-singleton wiring.

HugoswayAdapter is NOT exercised here — its selectors are a live-only
hypothesis and the LIVE_TRADING_ENABLED gate means production flows
never reach it. Tests that do mention it only assert construction
doesn't blow up and that ``verify_session`` refuses off-Hugosway URLs.
"""

from __future__ import annotations

import pytest

from core.trading.xauusd.broker import (
    AccountInfo,
    BrokerError,
    HugoswayAdapter,
    MockBroker,
    OrderRequest,
    get_broker,
    set_broker,
)


@pytest.fixture(autouse=True)
def _reset():
    set_broker(None)
    yield
    set_broker(None)


@pytest.mark.asyncio
async def test_mockbroker_verify_session_returns_accountinfo() -> None:
    broker = MockBroker(balance_usd=500.0, leverage=300)
    info = await broker.verify_session()
    assert isinstance(info, AccountInfo)
    assert info.balance_usd == 500.0
    assert info.leverage == 300
    assert info.connected is True


@pytest.mark.asyncio
async def test_mockbroker_verify_session_raises_when_configured() -> None:
    broker = MockBroker(raise_on_verify=BrokerError("x", kind="session"))
    with pytest.raises(BrokerError) as ei:
        await broker.verify_session()
    assert ei.value.kind == "session"


@pytest.mark.asyncio
async def test_mockbroker_place_order_records_and_adds_position() -> None:
    broker = MockBroker(balance_usd=500.0)
    req = OrderRequest(
        side="LONG",
        lots=0.05,
        order_type="LIMIT",
        limit_price=2400.0,
        stop_loss_price=2395.0,
        take_profit_price=2410.0,
    )
    res = await broker.place_order(req)
    assert res.placed is True
    assert res.order_id is not None
    positions = await broker.get_open_positions()
    assert len(positions) == 1
    assert positions[0].side == "LONG"
    assert positions[0].entry_price == pytest.approx(2400.0)
    assert positions[0].stop_price == pytest.approx(2395.0)
    assert positions[0].take_profit_price == pytest.approx(2410.0)
    assert broker.last_order == req


@pytest.mark.asyncio
async def test_mockbroker_place_order_raises_when_configured() -> None:
    broker = MockBroker(raise_on_place=BrokerError("no margin", kind="broker"))
    with pytest.raises(BrokerError):
        await broker.place_order(
            OrderRequest(side="LONG", lots=0.05, order_type="MARKET")
        )


@pytest.mark.asyncio
async def test_mockbroker_close_position_removes() -> None:
    broker = MockBroker()
    await broker.place_order(
        OrderRequest(
            side="LONG",
            lots=0.05,
            order_type="LIMIT",
            limit_price=2400.0,
        )
    )
    pid = (await broker.get_open_positions())[0].position_id
    result = await broker.close_position(pid)
    assert result.placed is True
    assert await broker.get_open_positions() == []


@pytest.mark.asyncio
async def test_mockbroker_close_position_misses_cleanly() -> None:
    broker = MockBroker()
    result = await broker.close_position("does-not-exist")
    assert result.placed is False
    assert "not found" in result.message


@pytest.mark.asyncio
async def test_mockbroker_close_all_closes_each() -> None:
    broker = MockBroker()
    for price in (2400.0, 2401.0, 2402.0):
        await broker.place_order(
            OrderRequest(
                side="LONG",
                lots=0.05,
                order_type="LIMIT",
                limit_price=price,
            )
        )
    results = await broker.close_all_positions()
    assert len(results) == 3
    assert all(r.placed for r in results)
    assert await broker.get_open_positions() == []


@pytest.mark.asyncio
async def test_account_info_derives_pnl_equity_margin() -> None:
    from core.trading.xauusd.broker import OpenPosition

    broker = MockBroker(
        balance_usd=500.0,
        leverage=300,
        positions=[
            OpenPosition(
                position_id="p1",
                side="LONG",
                lots=0.05,
                entry_price=2400.0,
                stop_price=None,
                take_profit_price=None,
                current_pnl_usd=25.0,
            )
        ],
    )
    info = await broker.get_account_info()
    assert info.pnl_usd == 25.0
    assert info.equity_usd == pytest.approx(525.0)
    # margin ≈ entry * lots / leverage = 2400 * 0.05 / 300
    assert info.margin_usd == pytest.approx(0.4)
    assert info.margin_level is not None and info.margin_level > 0


# ── Broker singleton ──────────────────────────────────────────────

def test_set_get_broker_round_trip() -> None:
    assert get_broker() is None
    b = MockBroker()
    set_broker(b)
    assert get_broker() is b
    set_broker(None)
    assert get_broker() is None


# ── HugoswayAdapter guards (no DOM exercise) ──────────────────────


class _FakePage:
    """Stand-in Playwright page. Only ``url`` is read by verify_session."""

    def __init__(self, url: str) -> None:
        self.url = url


@pytest.mark.asyncio
async def test_hugosway_verify_rejects_non_hugosway_url() -> None:
    adapter = HugoswayAdapter(
        page=_FakePage("https://example.com/trade"),
        session_id="s-1",
        account_type="demo",
    )
    with pytest.raises(BrokerError) as ei:
        await adapter.verify_session()
    assert ei.value.kind == "session"


@pytest.mark.asyncio
async def test_hugosway_close_position_is_not_implemented() -> None:
    adapter = HugoswayAdapter(
        page=_FakePage("https://trade.hugosway.com"),
        session_id="s-1",
        account_type="demo",
    )
    with pytest.raises(BrokerError) as ei:
        await adapter.close_position("whatever")
    assert ei.value.kind == "broker"
    assert "PR C-2" in str(ei.value)


@pytest.mark.asyncio
async def test_hugosway_close_all_is_not_implemented() -> None:
    adapter = HugoswayAdapter(
        page=_FakePage("https://trade.hugosway.com"),
        session_id="s-1",
        account_type="demo",
    )
    with pytest.raises(BrokerError) as ei:
        await adapter.close_all_positions()
    assert ei.value.kind == "broker"
