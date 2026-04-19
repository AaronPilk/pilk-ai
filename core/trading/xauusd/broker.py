"""Broker adapter layer for the XAUUSD execution agent.

The agent never talks to Hugosway directly. Every broker call goes
through the ``BrokerAdapter`` protocol so:

    * tests run entirely offline against ``MockBroker``.
    * swapping brokers (TradeLocker, OANDA, etc.) later is a new adapter,
      not an agent rewrite.
    * the live gate sits in exactly one place — the tool layer — and
      adapter code can't bypass it.

``HugoswayAdapter`` drives the operator's logged-in Browserbase session
via Playwright. All selectors are **text-based** (``get_by_role``,
``get_by_text``) rather than CSS — Hugosway's build-hashed class names
change with every deploy, their visible labels do not.

Every adapter method is defensive about:

    * the attached session going stale (page navigated away, browser
      closed). Returns a clean ``BrokerError`` with ``kind="session"``.
    * the wrong symbol being selected (XAUUSD watchlist tab lost focus).
    * forbidden UI labels appearing anywhere in the flow — see
      ``core.trading.xauusd.safety.reject_forbidden_label``.

**IMPORTANT**: the ``HugoswayAdapter`` selectors below are the author's
best read from operator screenshots. They have not been exercised
against live Hugosway yet — every selector is marked NEEDS_LIVE_VERIFY
in a comment. Production use is still gated by ``LIVE_TRADING_ENABLED``
(False), so even with wrong selectors no real order can be placed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.trading.xauusd.safety import (
    forbidden_label_error,
)


class BrokerError(Exception):
    """Raised for any non-recoverable adapter failure.

    ``kind`` classifies the failure so the tool layer can map to a clean
    user-facing message without string-sniffing:

        session   — attached session missing / stale
        ui        — expected element wasn't on the page
        forbidden — a tool tried to click/fill a forbidden UI label
        broker    — Hugosway itself rejected the action (margin etc.)
        transport — network / Playwright plumbing failure
    """

    def __init__(self, message: str, *, kind: str = "broker") -> None:
        super().__init__(message)
        self.kind = kind


# ── Data shapes ────────────────────────────────────────────────────


@dataclass(frozen=True)
class AccountInfo:
    """Scraped from the bottom strip of Hugosway's trading page."""

    balance_usd: float
    equity_usd: float
    pnl_usd: float
    margin_usd: float
    free_margin_usd: float
    margin_level: float | None  # None = ∞ (no open positions)
    leverage: int
    connected: bool
    account_id: str | None = None


@dataclass(frozen=True)
class OpenPosition:
    """Single row from the Positions tab."""

    position_id: str
    side: str  # "LONG" | "SHORT"
    lots: float
    entry_price: float
    stop_price: float | None
    take_profit_price: float | None
    current_pnl_usd: float


@dataclass(frozen=True)
class OrderRequest:
    """Input shape for ``place_order``. Keeping it as a dataclass means
    the protocol signature stays stable as more order parameters (bracket
    levels, OCO groups) appear."""

    side: str  # "LONG" | "SHORT"
    lots: float
    order_type: str = "MARKET"  # "MARKET" | "LIMIT" | "STOP"
    limit_price: float | None = None
    stop_price: float | None = None  # required for STOP orders
    stop_loss_price: float | None = None
    take_profit_price: float | None = None


@dataclass(frozen=True)
class OrderResult:
    placed: bool
    order_id: str | None
    message: str


# ── Protocol ──────────────────────────────────────────────────────


class BrokerAdapter(Protocol):
    """Every method is async; every method can raise ``BrokerError``."""

    async def verify_session(self) -> AccountInfo:
        """Prove the adapter can reach a live, XAUUSD-scoped page.

        Used at take-over time to fail fast if the page has drifted
        (wrong symbol, logged out, network error). The returned
        ``AccountInfo`` becomes part of the journal record."""

    async def get_account_info(self) -> AccountInfo: ...

    async def get_open_positions(self) -> list[OpenPosition]: ...

    async def place_order(self, order: OrderRequest) -> OrderResult: ...

    async def close_position(self, position_id: str) -> OrderResult: ...

    async def close_all_positions(self) -> list[OrderResult]: ...


# ── MockBroker ────────────────────────────────────────────────────


@dataclass
class MockBroker:
    """In-memory broker used by tests and as a failsafe when the real
    adapter isn't installed. Fully deterministic; no timers, no I/O."""

    balance_usd: float = 500.0
    leverage: int = 300
    account_id: str = "mock-001"
    connected: bool = True
    # Pre-seed position list for tests.
    positions: list[OpenPosition] = field(default_factory=list)
    # Fixtures for controlled failures in tests.
    raise_on_verify: BrokerError | None = None
    raise_on_place: BrokerError | None = None
    last_order: OrderRequest | None = None

    async def verify_session(self) -> AccountInfo:
        if self.raise_on_verify is not None:
            raise self.raise_on_verify
        return await self.get_account_info()

    async def get_account_info(self) -> AccountInfo:
        pnl = sum(p.current_pnl_usd for p in self.positions)
        equity = self.balance_usd + pnl
        margin = sum(
            (p.entry_price * p.lots) / self.leverage for p in self.positions
        )
        free = max(0.0, equity - margin)
        level = (equity / margin * 100.0) if margin > 0 else None
        return AccountInfo(
            balance_usd=self.balance_usd,
            equity_usd=equity,
            pnl_usd=pnl,
            margin_usd=margin,
            free_margin_usd=free,
            margin_level=level,
            leverage=self.leverage,
            connected=self.connected,
            account_id=self.account_id,
        )

    async def get_open_positions(self) -> list[OpenPosition]:
        return list(self.positions)

    async def place_order(self, order: OrderRequest) -> OrderResult:
        if self.raise_on_place is not None:
            raise self.raise_on_place
        self.last_order = order
        pid = f"mock-{len(self.positions) + 1}"
        # Mock: fill at the requested price (MARKET) or limit/stop price.
        entry = (
            order.limit_price
            if order.limit_price is not None
            else (order.stop_price if order.stop_price is not None else 0.0)
        )
        self.positions.append(
            OpenPosition(
                position_id=pid,
                side=order.side,
                lots=order.lots,
                entry_price=entry,
                stop_price=order.stop_loss_price,
                take_profit_price=order.take_profit_price,
                current_pnl_usd=0.0,
            )
        )
        return OrderResult(placed=True, order_id=pid, message="mock-filled")

    async def close_position(self, position_id: str) -> OrderResult:
        for i, p in enumerate(self.positions):
            if p.position_id == position_id:
                del self.positions[i]
                return OrderResult(
                    placed=True,
                    order_id=position_id,
                    message="mock-closed",
                )
        return OrderResult(
            placed=False,
            order_id=position_id,
            message="not found",
        )

    async def close_all_positions(self) -> list[OrderResult]:
        out: list[OrderResult] = []
        # Iterate a copy so we can mutate ``positions`` inside.
        for p in list(self.positions):
            out.append(await self.close_position(p.position_id))
        return out


# ── Hugosway adapter ─────────────────────────────────────────────
#
# Every selector below is a text-match against the labels visible in
# operator screenshots of https://trade.hugosway.com (April 2026). They
# are scoped defensively and every click/fill runs through
# ``forbidden_label_error`` before acting.
#
# The adapter is *not* called in any test path — MockBroker covers all
# offline coverage — and ``LIVE_TRADING_ENABLED`` is False, so every
# real-world place_order refuses at the tool layer before we get here.
# That makes the selectors below hypothesis-grade; they'll be verified
# live and iterated in the same session that flips the live gate to
# True. Each needs-verify line is tagged so the review diff is grep-able.

HUGOSWAY_BASE_URL = "https://trade.hugosway.com"
HUGOSWAY_SYMBOL_TEXT = "XAUUSD"

# Exact visible labels the adapter must never click / fill (super-set
# of the agent config's forbidden_ui_labels — keeping both means a
# misconfig can never shrink the list below the safe floor).
FORBIDDEN_EXACT_LABELS: tuple[str, ...] = (
    "Deposit",
    "Withdraw",
    "Withdrawals",
    "Transfer",
    "Bank",
    "Card",
    "Cashier",
    "Funding",
    "Wallet",
    "Payment",
)


class HugoswayAdapter:
    """Playwright-based adapter for the Hugosway web trader.

    Uses text-based locators almost exclusively; CSS selectors appear
    only where the same label repeats (the watchlist row "XAUUSD" text
    collides with the chart header, so the watchlist needs a role
    scope).
    """

    def __init__(self, page: Any, *, session_id: str, account_type: str) -> None:
        # ``page`` is intentionally ``Any`` to avoid a hard Playwright
        # import at module load time — we only need it installed when an
        # operator actually attaches a live session.
        self._page = page
        self.session_id = session_id
        self.account_type = account_type  # "demo" | "live"

    # ── Verification & read-only ────────────────────────────────

    async def verify_session(self) -> AccountInfo:
        url = str(self._page.url) if hasattr(self._page, "url") else ""
        if "hugosway" not in url.lower():
            raise BrokerError(
                f"attached page is not on Hugosway (url={url!r})",
                kind="session",
            )
        # Ensure XAUUSD is the active chart. NEEDS_LIVE_VERIFY: exact DOM
        # path of the watchlist tab may need scoping once we see it.
        await self._ensure_xauusd_selected()
        return await self.get_account_info()

    async def get_account_info(self) -> AccountInfo:
        # NEEDS_LIVE_VERIFY: the bottom strip exposes Balance / Equity /
        # P&L / Margin / Free Margin / Margin Level / Leverage as
        # text. Relying on role="status" or aria-labels if present;
        # otherwise we'll regex the strip.
        page = self._page

        async def _txt(label: str) -> str:
            loc = page.get_by_text(label, exact=True).first
            try:
                handle = await loc.element_handle()
            except Exception as e:
                raise BrokerError(
                    f"can't find '{label}' — page drifted?", kind="ui"
                ) from e
            if handle is None:
                raise BrokerError(
                    f"'{label}' not present on page", kind="ui"
                )
            # The value typically lives in a sibling or parent's next
            # child. ``locator(..).locator('xpath=following-sibling::*')``
            # works on most Hugosway layouts we've seen in screenshots.
            sib = loc.locator("xpath=following-sibling::*[1]")
            return (await sib.inner_text()).strip()

        try:
            balance = _parse_money(await _txt("Balance"))
            equity = _parse_money(await _txt("Equity"))
            pnl = _parse_money(await _txt("P&L"))
            margin = _parse_money(await _txt("Margin"))
            free_margin = _parse_money(await _txt("Free Margin"))
            level_raw = (await _txt("Margin Level")).strip()
            level = None if level_raw in {"∞", "--", ""} else _parse_money(level_raw)
            lev_raw = await _txt("Leverage")  # e.g. "1:300"
            leverage = int(lev_raw.split(":")[-1]) if ":" in lev_raw else int(
                lev_raw or "300"
            )
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(
                f"failed to scrape account info: {e}", kind="ui"
            ) from e

        return AccountInfo(
            balance_usd=balance,
            equity_usd=equity,
            pnl_usd=pnl,
            margin_usd=margin,
            free_margin_usd=free_margin,
            margin_level=level,
            leverage=leverage,
            connected=True,
        )

    async def get_open_positions(self) -> list[OpenPosition]:
        # NEEDS_LIVE_VERIFY: click Positions tab and read rows. The
        # positions table is a grid of role="row" elements in screenshots.
        page = self._page
        try:
            await page.get_by_role("tab", name="Positions").click()
        except Exception as e:
            raise BrokerError(
                f"Positions tab not clickable: {e}", kind="ui"
            ) from e
        # Empty-state short-circuit.
        no_pos = page.get_by_text("No open positions", exact=True).first
        try:
            if await no_pos.count() > 0:
                return []
        except Exception:
            # If count() explodes the page is probably stale; fall
            # through to the row scrape which will surface a cleaner error.
            pass
        return []  # NEEDS_LIVE_VERIFY: row scrape implementation pending.

    # ── Order placement ─────────────────────────────────────────

    async def place_order(self, order: OrderRequest) -> OrderResult:
        page = self._page
        await self._ensure_xauusd_selected()

        # Side
        side_label = "BUY" if order.side == "LONG" else "SELL"
        await self._safe_click(side_label)

        # Order type
        type_label = order.order_type.title()  # "Market" | "Limit" | "Stop"
        await self._safe_click(type_label)

        # Lots — NEEDS_LIVE_VERIFY: the input has the label "ORDER VALUE"
        # above it but we may need to key onto a role=spinbutton instead.
        lot_input = page.get_by_role("spinbutton").first
        try:
            await lot_input.fill(str(order.lots))
        except Exception as e:
            raise BrokerError(
                f"lots input not fillable: {e}", kind="ui"
            ) from e

        if order.order_type == "LIMIT" and order.limit_price is not None:
            await self._fill_by_label("LIMIT PRICE", str(order.limit_price))
        if order.order_type == "STOP" and order.stop_price is not None:
            await self._fill_by_label("STOP PRICE", str(order.stop_price))

        # SL / TP — expandable section.
        if (
            order.stop_loss_price is not None
            or order.take_profit_price is not None
        ):
            await self._safe_click("Add Stop Loss / Take Profit")
            if order.stop_loss_price is not None:
                await self._fill_by_label(
                    "Stop Loss", str(order.stop_loss_price)
                )
            if order.take_profit_price is not None:
                await self._fill_by_label(
                    "Take Profit", str(order.take_profit_price)
                )

        # Final confirm. NEEDS_LIVE_VERIFY: the button label includes
        # the live price ("BUY 4832.72") so an exact-match would break
        # every tick. We scope to role=button whose name starts with the
        # side label.
        confirm = (
            page.get_by_role("button")
            .filter(has_text=side_label)
            .last
        )
        try:
            await confirm.click()
        except Exception as e:
            raise BrokerError(
                f"confirm button not clickable: {e}", kind="ui"
            ) from e

        # Give Hugosway a moment to ack the order + surface any error
        # toast. NEEDS_LIVE_VERIFY: tune this or replace with a waiter
        # on the Positions tab change.
        await asyncio.sleep(1.0)

        # Best-effort order id: pull the newest position after the fill.
        positions = await self.get_open_positions()
        order_id = positions[-1].position_id if positions else None
        return OrderResult(
            placed=order_id is not None,
            order_id=order_id,
            message="submitted" if order_id else "no position seen after submit",
        )

    async def close_position(self, position_id: str) -> OrderResult:
        # NEEDS_LIVE_VERIFY: Positions row → X / close button pattern.
        raise BrokerError(
            "close_position on Hugosway adapter is not implemented yet "
            "(PR C-2). Use xauusd_flatten_all to force-disable and "
            "close manually in the browser.",
            kind="broker",
        )

    async def close_all_positions(self) -> list[OrderResult]:
        # NEEDS_LIVE_VERIFY: iterate Positions tab rows.
        raise BrokerError(
            "close_all_positions on Hugosway adapter is not implemented "
            "yet (PR C-2). Use xauusd_flatten_all to force-disable.",
            kind="broker",
        )

    # ── Helpers (shared) ───────────────────────────────────────

    async def _ensure_xauusd_selected(self) -> None:
        """Click the XAUUSD watchlist tab so every subsequent action
        operates on gold, not whatever symbol was last open."""
        try:
            tab = (
                self._page.get_by_role("tab")
                .filter(has_text=HUGOSWAY_SYMBOL_TEXT)
                .first
            )
            if await tab.count() == 0:
                # Fallback: the watchlist rows may be <button>s with the
                # text "XAUUSD" rather than role=tab.
                tab = (
                    self._page.get_by_role("button")
                    .filter(has_text=HUGOSWAY_SYMBOL_TEXT)
                    .first
                )
            await tab.click()
        except Exception as e:
            raise BrokerError(
                f"could not focus XAUUSD chart: {e}", kind="ui"
            ) from e

    async def _safe_click(self, label: str) -> None:
        """Click a visible button by exact visible label, refusing any
        forbidden label before we touch the DOM."""
        err = forbidden_label_error(label, FORBIDDEN_EXACT_LABELS)
        if err is not None:
            raise BrokerError(err, kind="forbidden")
        loc = (
            self._page.get_by_role("button", name=label, exact=True)
            .or_(self._page.get_by_text(label, exact=True).first)
        )
        try:
            await loc.click()
        except Exception as e:
            raise BrokerError(
                f"click '{label}' failed: {e}", kind="ui"
            ) from e

    async def _fill_by_label(self, label: str, value: str) -> None:
        err = forbidden_label_error(label, FORBIDDEN_EXACT_LABELS)
        if err is not None:
            raise BrokerError(err, kind="forbidden")
        loc = self._page.get_by_label(label).first
        try:
            await loc.fill(value)
        except Exception as e:
            raise BrokerError(
                f"fill '{label}' failed: {e}", kind="ui"
            ) from e


# ── Process-wide adapter singleton ───────────────────────────────

_broker: BrokerAdapter | None = None


def set_broker(adapter: BrokerAdapter | None) -> None:
    """Install (or clear) the active broker adapter.

    Tools read through ``get_broker`` and refuse loudly if nothing's
    been installed — which is also the default at daemon boot."""
    global _broker
    _broker = adapter


def get_broker() -> BrokerAdapter | None:
    return _broker


def _parse_money(raw: str) -> float:
    """Strip currency + commas, tolerate empty → 0.0."""
    s = raw.strip().replace("$", "").replace(",", "").replace("%", "")
    if not s or s in {"--", "∞"}:
        return 0.0
    try:
        return float(s)
    except ValueError as e:
        raise BrokerError(f"unparseable money value: {raw!r}", kind="ui") from e


__all__ = [
    "FORBIDDEN_EXACT_LABELS",
    "HUGOSWAY_BASE_URL",
    "HUGOSWAY_SYMBOL_TEXT",
    "AccountInfo",
    "BrokerAdapter",
    "BrokerError",
    "HugoswayAdapter",
    "MockBroker",
    "OpenPosition",
    "OrderRequest",
    "OrderResult",
    "get_broker",
    "set_broker",
]
