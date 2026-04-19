"""Public surface of the XAU/USD trading engine.

Everything in here is pure Python — no I/O, no network, no orders.
The PILK tool layer (``core/tools/builtin/xauusd.py``) wraps these
primitives into the agent's external interface.

Live execution is hard-gated: ``LIVE_TRADING_ENABLED`` is a module
constant in ``config.py`` that is ``False`` until a human opens that
file and flips it to True — and that flip must ride alongside a fully
tested broker adapter in the same PR. No UI toggle, no runtime flag,
no environment variable can override it.
"""

from core.trading.xauusd.candle import Candle, closes, highs, last_n, lows
from core.trading.xauusd.config import (
    ALLOWED_SYMBOLS,
    DEFAULT_CONFIG,
    LIVE_TRADING_ENABLED,
    XAUUSDConfig,
)
from core.trading.xauusd.indicators import adx, ema, rsi, slope
from core.trading.xauusd.risk import (
    PositionSize,
    SizingRefusal,
    apply_daily_loss_gate,
    apply_drawdown_gate,
    position_size_for_risk,
)
from core.trading.xauusd.rules import (
    Bias,
    Evaluation,
    Verdict,
    evaluate_setup,
)
from core.trading.xauusd.state import (
    AgentState,
    IllegalTransitionError,
    StateMachine,
    StateTransition,
)
from core.trading.xauusd.structure import (
    Pivot,
    PivotKind,
    Regime,
    RegimeSnapshot,
    StructureLabel,
    classify_regime,
    swing_points,
    trend_structure,
)

__all__ = [
    "ALLOWED_SYMBOLS",
    "DEFAULT_CONFIG",
    "LIVE_TRADING_ENABLED",
    "AgentState",
    "Bias",
    "Candle",
    "Evaluation",
    "IllegalTransitionError",
    "Pivot",
    "PivotKind",
    "PositionSize",
    "Regime",
    "RegimeSnapshot",
    "SizingRefusal",
    "StateMachine",
    "StateTransition",
    "StructureLabel",
    "Verdict",
    "XAUUSDConfig",
    "adx",
    "apply_daily_loss_gate",
    "apply_drawdown_gate",
    "classify_regime",
    "closes",
    "ema",
    "evaluate_setup",
    "highs",
    "last_n",
    "lows",
    "position_size_for_risk",
    "rsi",
    "slope",
    "swing_points",
    "trend_structure",
]
