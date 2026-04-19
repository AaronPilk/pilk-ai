"""Tool surface for the XAUUSD execution agent.

Everything here is *paper-mode only*. The two execution tools
(``xauusd_place_order``, ``xauusd_flatten_all``) hard-refuse to run
unless ``LIVE_TRADING_ENABLED`` in ``core.trading.xauusd.config`` is
True AND a Hugosway Browserbase adapter has been implemented — both
land in a separate PR.

The evaluation / risk / state tools are fully functional now. They let
the agent analyze structure, size positions, and journal its decisions
against fixture or live-fed candle data. The broker / feed gaps are
explicit and loud.

Risk labels chosen so the approval gate treats this agent sanely:

    xauusd_evaluate, xauusd_calc_size, xauusd_state  → READ
    xauusd_get_candles                                → NET_READ
    xauusd_place_order, xauusd_flatten_all            → FINANCIAL
"""

from __future__ import annotations

from typing import Any

from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome
from core.trading.xauusd import (
    ALLOWED_SYMBOLS,
    DEFAULT_CONFIG,
    LIVE_TRADING_ENABLED,
    AgentState,
    Candle,
    StateMachine,
    evaluate_setup,
    position_size_for_risk,
)
from core.trading.xauusd.journal import (
    journal_order_attempt,
    journal_risk,
    journal_safety_interrupt,
    journal_state,
    journal_verdict,
)

# Process-local state machine — tools share it within one pilkd instance.
# Persisting across restarts lives in the Ledger + a dedicated migration
# (PR C). For now every daemon boot starts in OFF, which is the safe
# default.
_STATE = StateMachine()


def _enforce_symbol(symbol: str) -> str | None:
    """Return an error string iff the symbol is not XAU/USD."""
    if symbol.upper().replace("_", "") not in {s.upper().replace("_", "") for s in ALLOWED_SYMBOLS}:
        return (
            f"refused: only XAUUSD is allowed (got '{symbol}'). This "
            "agent is a single-instrument gold specialist."
        )
    return None


def _parse_candles(raw: list[dict[str, Any]]) -> list[Candle]:
    """Coerce a JSON-style list-of-dicts into Candle objects.

    Accepts keys in either short or long form so the LLM doesn't have
    to remember which: ``o/h/l/c/v`` or ``open/high/low/close/volume``.
    """
    out: list[Candle] = []
    for i, row in enumerate(raw):
        try:
            out.append(
                Candle(
                    ts=int(row.get("ts") or row.get("timestamp") or i),
                    open=float(row.get("o") or row.get("open") or 0.0),
                    high=float(row.get("h") or row.get("high") or 0.0),
                    low=float(row.get("l") or row.get("low") or 0.0),
                    close=float(row.get("c") or row.get("close") or 0.0),
                    volume=float(row.get("v") or row.get("volume") or 0.0),
                )
            )
        except (TypeError, ValueError) as e:
            raise ValueError(f"row {i} malformed: {e}") from e
    return out


# ── xauusd_evaluate ───────────────────────────────────────────────

async def _evaluate(args: dict, ctx: ToolContext) -> ToolOutcome:
    err = _enforce_symbol(str(args.get("symbol") or "XAUUSD"))
    if err:
        journal_safety_interrupt(reason=err, plan_id=ctx.plan_id)
        return ToolOutcome(content=err, is_error=True)
    try:
        c5 = _parse_candles(args.get("candles_5m") or [])
    except ValueError as e:
        return ToolOutcome(
            content=f"bad 5M candle payload: {e}",
            is_error=True,
        )
    c1 = _parse_candles(args.get("candles_1m") or []) or None
    c15 = _parse_candles(args.get("candles_15m") or []) or None
    c1h = _parse_candles(args.get("candles_1h") or []) or None
    c4h = _parse_candles(args.get("candles_4h") or []) or None
    spread = float(args.get("spread_usd") or 0.0)

    ev = evaluate_setup(
        config=DEFAULT_CONFIG,
        candles_5m=c5,
        candles_1m=c1,
        candles_15m=c15,
        candles_1h=c1h,
        candles_4h=c4h,
        spread_usd=spread,
    )
    journal_verdict(
        verdict=ev.verdict,
        reason=ev.reason,
        details=ev.details,
        plan_id=ctx.plan_id,
    )
    return ToolOutcome(
        content=f"verdict={ev.verdict} — {ev.reason}",
        data={"verdict": ev.verdict, "reason": ev.reason, "details": ev.details},
    )


xauusd_evaluate_tool = Tool(
    name="xauusd_evaluate",
    description=(
        "Evaluate an XAU/USD setup against the full top-down rule engine "
        "(MTF alignment, structure, EMA/RSI/ADX, candle confirmation, "
        "regime classification). Returns a structured verdict: "
        "TAKE_LONG, TAKE_SHORT, NO_TRADE, or DISABLED with a specific "
        "reason. Pure analysis — never places orders."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Must be XAUUSD / XAU/USD / GOLD.",
            },
            "candles_5m": {
                "type": "array",
                "description": (
                    "5-minute candles, oldest first. Each row: "
                    "{ts, open, high, low, close, volume}."
                ),
                "items": {"type": "object"},
            },
            "candles_1m": {"type": "array", "items": {"type": "object"}},
            "candles_15m": {"type": "array", "items": {"type": "object"}},
            "candles_1h": {"type": "array", "items": {"type": "object"}},
            "candles_4h": {"type": "array", "items": {"type": "object"}},
            "spread_usd": {
                "type": "number",
                "description": "Current XAU/USD spread in USD.",
            },
        },
        "required": ["candles_5m"],
    },
    risk=RiskClass.READ,
    handler=_evaluate,
)


# ── xauusd_calc_size ──────────────────────────────────────────────

async def _calc_size(args: dict, ctx: ToolContext) -> ToolOutcome:
    err = _enforce_symbol(str(args.get("symbol") or "XAUUSD"))
    if err:
        journal_safety_interrupt(reason=err, plan_id=ctx.plan_id)
        return ToolOutcome(content=err, is_error=True)
    try:
        equity = float(args["equity_usd"])
        entry = float(args["entry_price"])
        stop = float(args["stop_price"])
        spread = float(args.get("spread_usd") or 0.0)
    except (KeyError, ValueError) as e:
        return ToolOutcome(
            content=f"xauusd_calc_size missing required numeric args: {e}",
            is_error=True,
        )
    result = position_size_for_risk(
        config=DEFAULT_CONFIG,
        equity_usd=equity,
        entry_price=entry,
        stop_price=stop,
        spread_usd=spread,
    )
    # `result` is either a PositionSize (has `lots`) or a SizingRefusal
    # (has `reason`). Using duck-typing avoids an import ping-pong here.
    if hasattr(result, "lots"):
        journal_risk(
            accepted=True,
            reason="sized ok",
            lots=result.lots,
            risk_usd=result.risk_usd,
            stop_distance_usd=result.stop_distance_usd,
            plan_id=ctx.plan_id,
        )
        return ToolOutcome(
            content=(
                f"lots={result.lots} risk=${result.risk_usd} "
                f"stop=${result.stop_distance_usd} margin=${result.margin_usd}"
            ),
            data={
                "lots": result.lots,
                "risk_usd": result.risk_usd,
                "notional_usd": result.notional_usd,
                "margin_usd": result.margin_usd,
                "stop_distance_usd": result.stop_distance_usd,
                "spread_usd": result.spread_usd,
            },
        )
    # Refusal
    reason = getattr(result, "reason", "unknown refusal")
    journal_risk(accepted=False, reason=reason, plan_id=ctx.plan_id)
    return ToolOutcome(
        content=f"refused: {reason}",
        is_error=True,
        data={"refused": True, "reason": reason},
    )


xauusd_calc_size_tool = Tool(
    name="xauusd_calc_size",
    description=(
        "Compute XAU/USD position size given equity, entry, stop, and "
        "spread. Applies per-trade risk cap, margin cap, min/max stop "
        "distance gates. Returns concrete lot size or a structured "
        "refusal with reason. Pure math."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "equity_usd": {"type": "number", "minimum": 0},
            "entry_price": {"type": "number", "exclusiveMinimum": 0},
            "stop_price": {"type": "number", "exclusiveMinimum": 0},
            "spread_usd": {"type": "number", "minimum": 0},
        },
        "required": ["equity_usd", "entry_price", "stop_price"],
    },
    risk=RiskClass.READ,
    handler=_calc_size,
)


# ── xauusd_state ──────────────────────────────────────────────────

async def _state(args: dict, ctx: ToolContext) -> ToolOutcome:
    action = str(args.get("action") or "get")
    if action == "get":
        return ToolOutcome(
            content=f"state={_STATE.current.value}",
            data={
                "state": _STATE.current.value,
                "history": [
                    {
                        "from": t.from_state.value,
                        "to": t.to_state.value,
                        "reason": t.reason,
                        "at": t.at,
                    }
                    for t in _STATE.history[-20:]
                ],
            },
        )
    if action == "transition":
        try:
            target = AgentState(str(args["to"]).upper())
        except (KeyError, ValueError):
            return ToolOutcome(
                content=f"unknown target state: {args.get('to')}",
                is_error=True,
            )
        reason = str(args.get("reason") or "").strip()
        if not reason:
            return ToolOutcome(
                content="state transitions require a non-empty reason",
                is_error=True,
            )
        try:
            t = _STATE.transition(target, reason)
        except Exception as e:
            return ToolOutcome(
                content=f"illegal transition: {e}", is_error=True
            )
        journal_state(t, plan_id=ctx.plan_id)
        return ToolOutcome(
            content=f"transitioned {t.from_state.value} → {t.to_state.value}",
            data={
                "from": t.from_state.value,
                "to": t.to_state.value,
                "reason": t.reason,
                "at": t.at,
            },
        )
    if action == "disable":
        reason = str(args.get("reason") or "manual").strip() or "manual"
        t = _STATE.force_disable(reason)
        journal_state(t, plan_id=ctx.plan_id)
        journal_safety_interrupt(reason=reason, plan_id=ctx.plan_id)
        return ToolOutcome(
            content=f"DISABLED ({reason})",
            data={"state": _STATE.current.value, "reason": reason},
        )
    return ToolOutcome(
        content=f"unknown action '{action}'. Use get|transition|disable.",
        is_error=True,
    )


xauusd_state_tool = Tool(
    name="xauusd_state",
    description=(
        "Read or advance the XAUUSD agent's state machine. Actions: "
        "'get' returns current + last 20 transitions; 'transition' "
        "requires {to, reason} and rejects illegal transitions; "
        "'disable' force-moves to DISABLED and is always allowed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "transition", "disable"],
            },
            "to": {
                "type": "string",
                "description": (
                    "Target state: OFF, SCANNING, WATCHLIST, BIASED_LONG, "
                    "BIASED_SHORT, READY_LONG, READY_SHORT, IN_POSITION, "
                    "COOLDOWN, DISABLED."
                ),
            },
            "reason": {"type": "string"},
        },
        "required": ["action"],
    },
    risk=RiskClass.READ,
    handler=_state,
)


# ── Placeholders — implemented in follow-up PRs ───────────────────
#
# These exist so the agent's manifest tool allowlist is complete and
# the system prompt can reference real tool names. Each one returns a
# loud "not configured" error explaining which PR lands it.

async def _get_candles(args: dict, ctx: ToolContext) -> ToolOutcome:
    return ToolOutcome(
        content=(
            "xauusd_get_candles is not yet wired to a price feed. "
            "This is implemented in PR B (Twelve Data adapter). Until "
            "then, pass candle arrays directly to xauusd_evaluate."
        ),
        is_error=True,
    )


xauusd_get_candles_tool = Tool(
    name="xauusd_get_candles",
    description=(
        "Fetch recent XAU/USD candles for a given timeframe. Placeholder "
        "in this PR — returns a 'not configured' error; PR B wires Twelve "
        "Data as the feed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "timeframe": {
                "type": "string",
                "enum": ["1M", "5M", "15M", "1H", "4H"],
            },
            "count": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        "required": ["timeframe"],
    },
    risk=RiskClass.NET_READ,
    handler=_get_candles,
)


async def _place_order(args: dict, ctx: ToolContext) -> ToolOutcome:
    if not LIVE_TRADING_ENABLED:
        msg = (
            "xauusd_place_order refused: LIVE_TRADING_ENABLED is False. "
            "Flipping to live requires a code-level change in "
            "core/trading/xauusd/config.py AND a Hugosway Browserbase "
            "adapter (PR C). No runtime toggle."
        )
        journal_safety_interrupt(reason=msg, plan_id=ctx.plan_id)
        journal_order_attempt(
            side=str(args.get("side", "?")),
            lots=float(args.get("lots") or 0.0),
            entry=float(args.get("entry_price") or 0.0),
            stop=float(args.get("stop_price") or 0.0),
            take_profit=args.get("take_profit_price"),
            mode="PAPER",
            placed=False,
            broker_message="LIVE_TRADING_ENABLED=False",
            plan_id=ctx.plan_id,
        )
        return ToolOutcome(content=msg, is_error=True)
    # Unreachable in this PR. Exists so the shape is obvious to the
    # follow-up broker adapter author.
    return ToolOutcome(
        content="xauusd_place_order: broker adapter missing (PR C)",
        is_error=True,
    )


xauusd_place_order_tool = Tool(
    name="xauusd_place_order",
    description=(
        "Place an XAU/USD order on the sandboxed Hugosway session. "
        "DISABLED in this PR — returns a refusal until the Hugosway "
        "Browserbase adapter lands in PR C. Risk-class FINANCIAL so the "
        "approval gate pauses every call even after the adapter ships."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "side": {"type": "string", "enum": ["LONG", "SHORT"]},
            "lots": {"type": "number", "exclusiveMinimum": 0},
            "entry_price": {"type": "number", "exclusiveMinimum": 0},
            "stop_price": {"type": "number", "exclusiveMinimum": 0},
            "take_profit_price": {"type": "number", "exclusiveMinimum": 0},
        },
        "required": ["side", "lots", "entry_price", "stop_price"],
    },
    risk=RiskClass.FINANCIAL,
    handler=_place_order,
)


async def _flatten_all(args: dict, ctx: ToolContext) -> ToolOutcome:
    journal_safety_interrupt(
        reason=str(args.get("reason") or "manual flatten"),
        plan_id=ctx.plan_id,
    )
    # Always force-disable the state machine on a flatten call — even
    # if LIVE_TRADING_ENABLED is False, a flatten request means "stop
    # now." Better to over-apply this than under-apply.
    t = _STATE.force_disable(
        f"flatten_all: {args.get('reason') or 'manual'}"
    )
    journal_state(t, plan_id=ctx.plan_id)
    if not LIVE_TRADING_ENABLED:
        return ToolOutcome(
            content=(
                "xauusd_flatten_all: paper-mode — no live positions to close. "
                "State machine forced to DISABLED."
            ),
            data={"state": _STATE.current.value},
        )
    return ToolOutcome(
        content=(
            "xauusd_flatten_all: broker adapter missing (PR C). "
            "State machine forced to DISABLED."
        ),
        is_error=True,
        data={"state": _STATE.current.value},
    )


xauusd_flatten_all_tool = Tool(
    name="xauusd_flatten_all",
    description=(
        "Emergency stop: close every open XAU/USD position and force "
        "the agent to DISABLED. Safe to call in paper mode (no-op on "
        "broker; still disables). Always allowed regardless of state."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Short human explanation for the journal.",
            }
        },
    },
    risk=RiskClass.FINANCIAL,
    handler=_flatten_all,
)


XAUUSD_TOOLS: list[Tool] = [
    xauusd_evaluate_tool,
    xauusd_calc_size_tool,
    xauusd_state_tool,
    xauusd_get_candles_tool,
    xauusd_place_order_tool,
    xauusd_flatten_all_tool,
]


def reset_state_for_tests() -> None:
    """Test helper — pytest only. Zeros the process-local state."""
    global _STATE
    _STATE = StateMachine()


__all__ = [
    "XAUUSD_TOOLS",
    "reset_state_for_tests",
    "xauusd_calc_size_tool",
    "xauusd_evaluate_tool",
    "xauusd_flatten_all_tool",
    "xauusd_get_candles_tool",
    "xauusd_place_order_tool",
    "xauusd_state_tool",
]
