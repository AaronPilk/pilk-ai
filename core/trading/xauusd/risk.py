"""Position sizing for XAU/USD.

Every public function here is pure math — no state, no I/O. The risk
engine is called by the execution path with:

- the current account equity
- the configured per-trade risk %
- the entry price + stop-loss price the rule engine proposed
- the current spread

and returns either a concrete ``PositionSize`` (with lots, notional,
margin, risk in USD) or a refusal explaining why the trade was
rejected. The refusal is first-class — callers must never fall back
to a smaller-than-requested size just to get filled.

Gold-specific conventions used throughout:

- XAU/USD quote is in USD per troy ounce.
- 1 standard lot = 100 oz, so a $1.00 move on 1 lot = $100 P/L.
- Point value scales linearly: $1 move on ``lots`` = ``lots * 100``.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.trading.xauusd.config import XAUUSDConfig

# 1 standard XAU/USD lot moves $100 for every $1.00 change in price.
# Expressed as dollars of P/L per dollar of price-move per lot.
DOLLARS_PER_USD_MOVE_PER_LOT = 100.0


@dataclass(frozen=True)
class PositionSize:
    lots: float              # in standard lots (1.0 = 100 oz)
    risk_usd: float          # realized risk if stop hits (always positive)
    notional_usd: float      # full position value
    margin_usd: float        # estimated margin usage (1:leverage assumption)
    stop_distance_usd: float # |entry - stop|, always positive
    spread_usd: float        # raw spread consulted for slippage math


@dataclass(frozen=True)
class SizingRefusal:
    reason: str


def position_size_for_risk(
    *,
    config: XAUUSDConfig,
    equity_usd: float,
    entry_price: float,
    stop_price: float,
    spread_usd: float,
    leverage: float = 300.0,
    min_lot: float = 0.01,
    lot_step: float = 0.01,
) -> PositionSize | SizingRefusal:
    """Compute lots that risk ``max_risk_per_trade_pct`` of equity.

    Rejects up-front on any of:
      * equity below ``min_account_balance_to_continue``
      * spread exceeding ``max_spread_usd``
      * stop distance outside ``[min_stop_usd, max_stop_usd]``
      * entry == stop (zero-risk trade = invalid setup)
      * minimum broker lot still exceeding the risk cap

    Lot size is floored to ``lot_step`` (default 0.01). Margin is
    computed assuming linear 1:leverage — fine as a planning estimate;
    broker-reported margin is what actually matters at fill time.
    """
    if equity_usd <= 0:
        return SizingRefusal("equity_usd must be > 0")
    if equity_usd < config.min_account_balance_to_continue:
        return SizingRefusal(
            f"equity ${equity_usd:.2f} below floor "
            f"${config.min_account_balance_to_continue:.2f}"
        )
    if spread_usd > config.max_spread_usd:
        return SizingRefusal(
            f"spread ${spread_usd:.2f} exceeds cap "
            f"${config.max_spread_usd:.2f}"
        )
    if entry_price <= 0 or stop_price <= 0:
        return SizingRefusal("entry and stop must be positive prices")

    stop_distance = abs(entry_price - stop_price)
    if stop_distance == 0:
        return SizingRefusal("entry equals stop — no invalidation defined")
    if stop_distance < config.min_stop_usd:
        return SizingRefusal(
            f"stop distance ${stop_distance:.2f} below floor "
            f"${config.min_stop_usd:.2f}"
        )
    if stop_distance > config.max_stop_usd:
        return SizingRefusal(
            f"stop distance ${stop_distance:.2f} above ceiling "
            f"${config.max_stop_usd:.2f}"
        )

    # Target risk in USD for this one trade.
    target_risk = equity_usd * (config.max_risk_per_trade_pct / 100.0)
    # Dollars-per-lot lost if the full stop distance hits.
    risk_per_lot = stop_distance * DOLLARS_PER_USD_MOVE_PER_LOT
    raw_lots = target_risk / risk_per_lot
    lots = _floor_to_step(raw_lots, lot_step)
    if lots < min_lot:
        return SizingRefusal(
            f"target risk ${target_risk:.2f} below what {min_lot} lots "
            f"at ${stop_distance:.2f} stop implies "
            f"(${min_lot * risk_per_lot:.2f})"
        )

    realized_risk = lots * risk_per_lot
    notional = lots * 100.0 * entry_price
    margin = notional / leverage
    margin_cap = equity_usd * (config.max_margin_usage_pct / 100.0)
    if margin > margin_cap:
        return SizingRefusal(
            f"margin ${margin:.2f} would exceed cap "
            f"${margin_cap:.2f} ({config.max_margin_usage_pct:.1f}% of equity)"
        )

    return PositionSize(
        lots=round(lots, 2),
        risk_usd=round(realized_risk, 2),
        notional_usd=round(notional, 2),
        margin_usd=round(margin, 2),
        stop_distance_usd=round(stop_distance, 2),
        spread_usd=round(spread_usd, 2),
    )


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return (int(value / step)) * step


def apply_daily_loss_gate(
    *, equity_usd: float, starting_equity_usd: float, config: XAUUSDConfig
) -> bool:
    """True if today's P/L has breached the daily loss cap.

    The agent should consult this every decision tick; once it returns
    True, transition to DISABLED until the daily reset cron runs.
    """
    if starting_equity_usd <= 0:
        return True  # fail closed — we can't reason without a baseline
    loss_pct = (starting_equity_usd - equity_usd) / starting_equity_usd * 100.0
    return loss_pct >= config.max_daily_loss_pct


def apply_drawdown_gate(
    *, equity_usd: float, peak_equity_usd: float, config: XAUUSDConfig
) -> bool:
    """True if rolling drawdown from peak has breached the cap."""
    if peak_equity_usd <= 0:
        return True
    dd_pct = (peak_equity_usd - equity_usd) / peak_equity_usd * 100.0
    return dd_pct >= config.max_equity_drawdown_pct
