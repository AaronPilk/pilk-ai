"""Pure-math tests for the risk engine.

Every rejection path gets an explicit test so regressions show up the
moment a refusal is silently upgraded to "try a smaller size".
"""

from __future__ import annotations

from core.trading.xauusd.config import XAUUSDConfig
from core.trading.xauusd.risk import (
    PositionSize,
    SizingRefusal,
    apply_daily_loss_gate,
    apply_drawdown_gate,
    position_size_for_risk,
)

# ── acceptance + math ────────────────────────────────────────────


def test_accept_at_default_config() -> None:
    cfg = XAUUSDConfig()
    result = position_size_for_risk(
        config=cfg,
        equity_usd=10_000.0,
        entry_price=2400.0,
        stop_price=2397.0,  # $3 stop → inside the 2-8 range
        spread_usd=0.20,
    )
    assert isinstance(result, PositionSize)
    # Target risk = 10_000 * 0.005 = $50.
    # Risk per lot = $3 * $100/lot = $300.
    # Raw lots = 50/300 = 0.1666...
    # Floored to 0.01 step → 0.16.
    assert result.lots == 0.16
    # Realized risk = 0.16 * 300 = $48.
    assert result.risk_usd == 48.0
    assert result.stop_distance_usd == 3.0


def test_reject_stop_too_tight() -> None:
    cfg = XAUUSDConfig()
    out = position_size_for_risk(
        config=cfg,
        equity_usd=10_000.0,
        entry_price=2400.0,
        stop_price=2399.50,  # $0.50 stop → below 2.0 floor
        spread_usd=0.10,
    )
    assert isinstance(out, SizingRefusal)
    assert "below floor" in out.reason


def test_reject_stop_too_wide() -> None:
    cfg = XAUUSDConfig()
    out = position_size_for_risk(
        config=cfg,
        equity_usd=10_000.0,
        entry_price=2400.0,
        stop_price=2390.0,  # $10 stop → above 8.0 ceiling
        spread_usd=0.10,
    )
    assert isinstance(out, SizingRefusal)
    assert "above ceiling" in out.reason


def test_reject_entry_equals_stop() -> None:
    cfg = XAUUSDConfig()
    out = position_size_for_risk(
        config=cfg,
        equity_usd=10_000.0,
        entry_price=2400.0,
        stop_price=2400.0,
        spread_usd=0.10,
    )
    assert isinstance(out, SizingRefusal)
    assert "no invalidation" in out.reason


def test_reject_spread_too_wide() -> None:
    cfg = XAUUSDConfig()
    out = position_size_for_risk(
        config=cfg,
        equity_usd=10_000.0,
        entry_price=2400.0,
        stop_price=2397.0,
        spread_usd=1.00,  # > 0.50 cap
    )
    assert isinstance(out, SizingRefusal)
    assert "spread" in out.reason


def test_reject_below_equity_floor() -> None:
    cfg = XAUUSDConfig()
    out = position_size_for_risk(
        config=cfg,
        equity_usd=100.0,  # below $250 floor
        entry_price=2400.0,
        stop_price=2397.0,
        spread_usd=0.10,
    )
    assert isinstance(out, SizingRefusal)
    assert "below floor" in out.reason


def test_reject_when_minimum_lot_exceeds_risk_cap() -> None:
    # Tiny account + wide-but-legal stop → even 0.01 lots breaches the
    # per-trade risk %. Must refuse, never shrink below min_lot.
    cfg = XAUUSDConfig(max_risk_per_trade_pct=0.1)
    out = position_size_for_risk(
        config=cfg,
        equity_usd=300.0,  # above $250 floor
        entry_price=2400.0,
        stop_price=2393.0,  # $7 stop
        spread_usd=0.10,
    )
    # Target risk = 300 * 0.001 = $0.30.
    # Risk per lot = 7 * 100 = $700. Raw lots = 0.00043 → below 0.01.
    assert isinstance(out, SizingRefusal)
    assert "below what" in out.reason.lower() or "below" in out.reason.lower()


def test_reject_when_margin_cap_exceeded() -> None:
    # Force margin over cap by shrinking the cap aggressively.
    cfg = XAUUSDConfig(max_margin_usage_pct=0.01)
    out = position_size_for_risk(
        config=cfg,
        equity_usd=100_000.0,
        entry_price=2400.0,
        stop_price=2397.0,
        spread_usd=0.10,
    )
    assert isinstance(out, SizingRefusal)
    assert "margin" in out.reason.lower()


# ── portfolio-level gates ────────────────────────────────────────


def test_daily_loss_gate_trips_at_threshold() -> None:
    cfg = XAUUSDConfig(max_daily_loss_pct=3.0)
    assert not apply_daily_loss_gate(
        equity_usd=9800.0, starting_equity_usd=10_000.0, config=cfg
    )
    # 3% loss → gate fires
    assert apply_daily_loss_gate(
        equity_usd=9699.99, starting_equity_usd=10_000.0, config=cfg
    )


def test_drawdown_gate_trips_at_threshold() -> None:
    cfg = XAUUSDConfig(max_equity_drawdown_pct=10.0)
    assert not apply_drawdown_gate(
        equity_usd=9500.0, peak_equity_usd=10_000.0, config=cfg
    )
    assert apply_drawdown_gate(
        equity_usd=8999.99, peak_equity_usd=10_000.0, config=cfg
    )


def test_gates_fail_closed_on_zero_baseline() -> None:
    cfg = XAUUSDConfig()
    assert apply_daily_loss_gate(
        equity_usd=100.0, starting_equity_usd=0.0, config=cfg
    )
    assert apply_drawdown_gate(
        equity_usd=100.0, peak_equity_usd=0.0, config=cfg
    )
