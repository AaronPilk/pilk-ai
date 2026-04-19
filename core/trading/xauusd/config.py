"""Runtime configuration for the XAUUSD execution agent.

Every knob the rule engine, risk engine, and state machine consult
lives here. Safe, paper-mode-friendly defaults — flipping any limit
higher is an explicit, reviewable edit rather than a hidden setting.

The module-level `LIVE_TRADING_ENABLED = False` is the last line of
defence: even if a config instance says ``live=True``, no tool will
touch a real order-placement codepath until this constant flips. That
flip is a deliberate code change reviewed in a separate PR, **never**
a UI toggle.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── Hard-coded safety gate ────────────────────────────────────────
#
# The XAUUSD agent is paper-mode-only until a human opens this file
# and flips this constant to True AND the Hugosway Browserbase adapter
# has been implemented + reviewed in a follow-up PR. No runtime code
# path can override this. If you're reading a code review diff that
# flips this to True, the accompanying commit MUST also include a
# fully tested broker adapter and a risk-officer sign-off.
LIVE_TRADING_ENABLED: bool = False

# The only symbol this agent may ever reason about. Anything else is
# a bug or a misconfigured upstream — the entrypoints enforce this.
ALLOWED_SYMBOLS: frozenset[str] = frozenset({"XAUUSD", "XAU/USD", "GOLD"})


@dataclass(frozen=True)
class XAUUSDConfig:
    """Immutable config snapshot for a single agent run.

    Defaults are deliberately conservative for XAU/USD at 1:300. Real
    account deployment starts from these numbers and can only relax
    them via an explicit edit in a reviewed PR.
    """

    # ── Mode ──────────────────────────────────────────────────
    mode: str = "PAPER"  # "PAPER" | "LIVE"
    allow_countertrend: bool = False

    # ── Timeframes ───────────────────────────────────────────
    primary_tf: str = "5M"
    entry_tf: str = "1M"
    bias_tf: str = "15M"
    htf_1: str = "1H"
    htf_2: str = "4H"
    require_mtf_alignment: bool = True
    require_htf_context: bool = True
    allow_1m_only_trades: bool = False

    # ── Position + risk caps ─────────────────────────────────
    max_open_trades: int = 3
    max_risk_per_trade_pct: float = 0.5      # % of equity per trade
    max_daily_loss_pct: float = 3.0          # auto-disable when hit
    max_equity_drawdown_pct: float = 10.0    # auto-disable when hit
    max_margin_usage_pct: float = 25.0       # hard cap vs equity
    min_account_balance_to_continue: float = 250.0

    # ── XAU/USD-specific execution filters ───────────────────
    # All price units are in instrument points, not ticks: XAU/USD
    # quotes to two decimals and moves in dollars per ounce.
    max_spread_usd: float = 0.50             # reject trade if spread > $0.50
    max_slippage_usd: float = 0.80           # abort fill if exceeded
    stop_buffer_usd: float = 1.20            # added beyond structure swing
    min_stop_usd: float = 2.00               # don't trade with stops tighter than this
    max_stop_usd: float = 8.00               # don't trade with stops wider than this

    # ── Trend filters ────────────────────────────────────────
    ema_fast_period: int = 50
    ema_slow_period: int = 200
    ema_slope_lookback: int = 10
    ema_slope_min_abs: float = 0.02          # points/candle — small, but nonzero
    rsi_period: int = 14
    rsi_long_support_min: float = 55.0
    rsi_short_support_max: float = 45.0
    adx_period: int = 14
    adx_min_trend: float = 18.0              # below this = no trend-entry

    # ── Structure filters ────────────────────────────────────
    swing_lookback: int = 5                  # candles on each side for pivot
    regime_lookback: int = 50                # candles used to classify regime

    # ── Anomaly / auto-disable thresholds ────────────────────
    anomaly_tick_jump_usd: float = 12.0      # a single-candle move this large = distorted
    anomaly_disable_minutes: int = 30        # how long to stay DISABLED after trip

    # ── Forbidden UI actions (Browserbase safety net) ─────────
    # Any tool driving the Hugosway session refuses to click / fill
    # anything whose label matches one of these strings, regardless of
    # what the LLM tries to do. This protects against prompt-injected
    # plan steps trying to move money outside of trading.
    forbidden_ui_labels: tuple[str, ...] = field(
        default_factory=lambda: (
            "withdraw",
            "deposit",
            "transfer",
            "bank",
            "card",
            "payment",
            "funding",
            "wallet",
            "cashier",
        )
    )

    def is_live(self) -> bool:
        """Both the code-level gate AND the config must agree."""
        return LIVE_TRADING_ENABLED and self.mode == "LIVE"


DEFAULT_CONFIG = XAUUSDConfig()
