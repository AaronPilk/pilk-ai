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

from core.config import get_settings
from core.policy.risk import RiskClass
from core.secrets import resolve_secret
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
from core.trading.xauusd.broker import (
    BrokerError,
    HugoswayAdapter,
    OrderRequest,
    get_broker,
    set_broker,
)
from core.trading.xauusd.feed import FeedError, TwelveDataFeed
from core.trading.xauusd.journal import (
    journal_broker_event,
    journal_order_attempt,
    journal_position_event,
    journal_risk,
    journal_safety_interrupt,
    journal_state,
    journal_verdict,
)
from core.trading.xauusd.session import (
    clear_attached_session,
    get_attached_session,
    set_attached_session,
)
from core.trading.xauusd.settings_store import (
    EXECUTION_MODES,
    get_execution_mode,
    set_execution_mode,
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
        mode = get_execution_mode()
        return ToolOutcome(
            content=f"state={_STATE.current.value} mode={mode}",
            data={
                "state": _STATE.current.value,
                "execution_mode": mode,
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
    if action == "get_mode":
        mode = get_execution_mode()
        return ToolOutcome(
            content=f"execution_mode={mode}",
            data={"execution_mode": mode},
        )
    if action == "set_mode":
        try:
            mode = set_execution_mode(str(args.get("mode") or ""))
        except ValueError as e:
            return ToolOutcome(content=f"refused: {e}", is_error=True)
        except RuntimeError as e:
            return ToolOutcome(content=f"unavailable: {e}", is_error=True)
        return ToolOutcome(
            content=f"execution_mode set to {mode}",
            data={"execution_mode": mode},
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
        "'get' returns current state + execution_mode + last 20 "
        "transitions; 'transition' requires {to, reason} and rejects "
        "illegal transitions; 'disable' force-moves to DISABLED and is "
        "always allowed; 'get_mode' / 'set_mode' read-or-write the "
        "execution_mode (approve | autonomous). In 'approve' mode every "
        "order request is queued for operator confirmation; in "
        "'autonomous' mode the agent trades within its risk caps without "
        "per-trade approval."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "get",
                    "transition",
                    "disable",
                    "get_mode",
                    "set_mode",
                ],
            },
            "to": {
                "type": "string",
                "description": (
                    "Target state: OFF, SCANNING, WATCHLIST, BIASED_LONG, "
                    "BIASED_SHORT, READY_LONG, READY_SHORT, IN_POSITION, "
                    "COOLDOWN, DISABLED."
                ),
            },
            "mode": {
                "type": "string",
                "enum": sorted(EXECUTION_MODES),
                "description": "Execution mode for action=set_mode.",
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
    tf = str(args.get("timeframe") or "").upper().strip()
    if not tf:
        return ToolOutcome(
            content="xauusd_get_candles requires a 'timeframe' argument.",
            is_error=True,
        )
    count = int(args.get("count") or 200)
    count = max(1, min(count, 500))

    api_key = resolve_secret(
        "twelvedata_api_key", get_settings().twelvedata_api_key
    )
    if not api_key:
        return ToolOutcome(
            content=(
                "Twelve Data is not configured. Paste your API key in "
                "Settings → API Keys → Twelve Data (free tier at "
                "twelvedata.com)."
            ),
            is_error=True,
        )

    feed = TwelveDataFeed(api_key)
    try:
        try:
            result = await feed.fetch_candles(tf, count)
        except FeedError as e:
            return ToolOutcome(
                content=f"xauusd_get_candles: {e}", is_error=True
            )
    finally:
        await feed.aclose()

    return ToolOutcome(
        content=(
            f"fetched {len(result.candles)} {result.timeframe} candles "
            f"({result.fetched_at or 'no server timestamp'})"
        ),
        data={
            "timeframe": result.timeframe,
            "count": len(result.candles),
            "fetched_at": result.fetched_at,
            "candles": [
                {
                    "ts": c.ts,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                }
                for c in result.candles
            ],
        },
    )


xauusd_get_candles_tool = Tool(
    name="xauusd_get_candles",
    description=(
        "Fetch recent XAU/USD candles from Twelve Data for a given "
        "timeframe (1M/5M/15M/1H/4H). Returns oldest-first bars ready "
        "for xauusd_evaluate. Requires twelvedata_api_key in Settings. "
        "Respects free-tier rate limits; surface errors rather than "
        "retry-loop on 429."
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


# ── Broker-bound tools (take_over, release, account_info, etc.) ──
#
# A single ``BrokerAdapter`` lives as a process-wide singleton, installed
# by ``xauusd_take_over`` and cleared by ``xauusd_release``. Every tool
# that hits the broker goes through ``get_broker()`` and refuses if
# nothing's attached — the operator *must* explicitly hand a session to
# the agent before any trading tool runs. That's the runtime permission
# model.


def _require_broker() -> tuple[Any, ToolOutcome | None]:
    """Return ``(adapter, None)`` when an adapter is installed; or
    ``(None, error_outcome)`` when one isn't.

    Keeps the boilerplate out of every handler without raising, which
    would bypass the ToolOutcome reporting path."""
    adapter = get_broker()
    if adapter is None:
        return None, ToolOutcome(
            content=(
                "no attached broker session. Call xauusd_take_over on "
                "a Browserbase session that's already logged into "
                "Hugosway and viewing the XAUUSD chart."
            ),
            is_error=True,
        )
    return adapter, None


async def _account_info(args: dict, ctx: ToolContext) -> ToolOutcome:
    adapter, err = _require_broker()
    if err is not None:
        return err
    try:
        info = await adapter.get_account_info()
    except BrokerError as e:
        journal_safety_interrupt(
            reason=f"account_info failed: {e}",
            payload={"kind": e.kind},
            plan_id=ctx.plan_id,
        )
        return ToolOutcome(content=f"broker error: {e}", is_error=True)
    return ToolOutcome(
        content=(
            f"balance=${info.balance_usd:.2f} equity=${info.equity_usd:.2f} "
            f"pnl=${info.pnl_usd:.2f} free=${info.free_margin_usd:.2f} "
            f"leverage=1:{info.leverage}"
        ),
        data={
            "balance_usd": info.balance_usd,
            "equity_usd": info.equity_usd,
            "pnl_usd": info.pnl_usd,
            "margin_usd": info.margin_usd,
            "free_margin_usd": info.free_margin_usd,
            "margin_level": info.margin_level,
            "leverage": info.leverage,
            "connected": info.connected,
            "account_id": info.account_id,
        },
    )


xauusd_account_info_tool = Tool(
    name="xauusd_account_info",
    description=(
        "Read account balance / equity / P&L / margin / leverage from "
        "the attached Hugosway session. Refuses if no session is "
        "attached — call xauusd_take_over first."
    ),
    input_schema={"type": "object", "properties": {}},
    risk=RiskClass.READ,
    handler=_account_info,
)


async def _open_positions(args: dict, ctx: ToolContext) -> ToolOutcome:
    adapter, err = _require_broker()
    if err is not None:
        return err
    try:
        positions = await adapter.get_open_positions()
    except BrokerError as e:
        return ToolOutcome(content=f"broker error: {e}", is_error=True)
    return ToolOutcome(
        content=f"{len(positions)} open position(s)",
        data={
            "count": len(positions),
            "positions": [
                {
                    "position_id": p.position_id,
                    "side": p.side,
                    "lots": p.lots,
                    "entry_price": p.entry_price,
                    "stop_price": p.stop_price,
                    "take_profit_price": p.take_profit_price,
                    "current_pnl_usd": p.current_pnl_usd,
                }
                for p in positions
            ],
        },
    )


xauusd_open_positions_tool = Tool(
    name="xauusd_open_positions",
    description=(
        "List every open XAU/USD position on the attached Hugosway "
        "session. Read-only. Returns empty list when nothing's open."
    ),
    input_schema={"type": "object", "properties": {}},
    risk=RiskClass.READ,
    handler=_open_positions,
)


# ── xauusd_release ───────────────────────────────────────────────


async def _release(args: dict, ctx: ToolContext) -> ToolOutcome:
    prev = clear_attached_session()
    set_broker(None)
    reason = str(args.get("reason") or "").strip() or "operator released"
    journal_broker_event(
        action="release",
        session_id=prev.session_id if prev else None,
        account_type=prev.account_type if prev else None,
        details={"reason": reason},
        plan_id=ctx.plan_id,
    )
    # Releasing always flips the state machine back to OFF (unless it's
    # already DISABLED, which is sticky). That's the safe default: next
    # take-over starts a fresh session clock.
    if _STATE.current is not AgentState.DISABLED:
        t = _STATE.force_disable(f"release: {reason}")
        journal_state(t, plan_id=ctx.plan_id)
    return ToolOutcome(
        content=(
            f"released session {prev.session_id if prev else 'none'}. "
            "State machine forced to DISABLED."
        ),
        data={
            "released_session": prev.session_id if prev else None,
            "state": _STATE.current.value,
        },
    )


xauusd_release_tool = Tool(
    name="xauusd_release",
    description=(
        "Detach the agent from the current Hugosway session. Forces "
        "the state machine to DISABLED. Always allowed. Run this at "
        "end-of-day or before closing the Browserbase window."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
        },
    },
    risk=RiskClass.FINANCIAL,
    handler=_release,
)


# ── xauusd_place_order ───────────────────────────────────────────


async def _place_order(args: dict, ctx: ToolContext) -> ToolOutcome:
    # Layer 1 — hard-coded Python constant.
    if not LIVE_TRADING_ENABLED:
        msg = (
            "xauusd_place_order refused: LIVE_TRADING_ENABLED is False. "
            "Flipping to live requires a reviewed code edit in "
            "core/trading/xauusd/config.py after the Hugosway adapter "
            "has been smoke-tested against a demo account. No runtime "
            "toggle overrides this."
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

    # Layer 2 — an adapter must be installed (take_over was called).
    adapter, err = _require_broker()
    if err is not None:
        journal_order_attempt(
            side=str(args.get("side", "?")),
            lots=float(args.get("lots") or 0.0),
            entry=float(args.get("entry_price") or 0.0),
            stop=float(args.get("stop_price") or 0.0),
            take_profit=args.get("take_profit_price"),
            mode="LIVE",
            placed=False,
            broker_message="no broker attached",
            plan_id=ctx.plan_id,
        )
        return err

    # Layer 3 — required args; state check would be next but is the
    # agent's responsibility (the tool can't know what structure say).
    try:
        side = str(args["side"]).upper()
        if side not in {"LONG", "SHORT"}:
            return ToolOutcome(
                content=f"invalid side '{side}'", is_error=True
            )
        lots = float(args["lots"])
        stop_price = float(args["stop_price"])
        entry_price = float(args.get("entry_price") or 0.0)
        order_type = str(args.get("order_type") or "MARKET").upper()
        take_profit = args.get("take_profit_price")
    except (KeyError, ValueError) as e:
        return ToolOutcome(
            content=f"xauusd_place_order missing/invalid args: {e}",
            is_error=True,
        )

    request = OrderRequest(
        side=side,
        lots=lots,
        order_type=order_type,
        limit_price=entry_price if order_type == "LIMIT" else None,
        stop_price=entry_price if order_type == "STOP" else None,
        stop_loss_price=stop_price,
        take_profit_price=float(take_profit) if take_profit else None,
    )

    # Layer 4 — execution_mode gate. In ``approve`` mode the tool layer
    # itself doesn't queue an approval — the outer Gateway does that via
    # RiskClass.FINANCIAL. In ``autonomous`` mode we still log the
    # decision but let the broker call proceed. Either way the risk
    # engine's caps apply (checked before this tool is called).
    mode = get_execution_mode()
    attached = get_attached_session()
    journal_broker_event(
        action="place_order_attempt",
        session_id=attached.session_id if attached else None,
        account_type=attached.account_type if attached else None,
        details={"execution_mode": mode, "side": side, "lots": lots},
        plan_id=ctx.plan_id,
    )

    try:
        result = await adapter.place_order(request)
    except BrokerError as e:
        journal_order_attempt(
            side=side,
            lots=lots,
            entry=entry_price,
            stop=stop_price,
            take_profit=take_profit,
            mode="LIVE",
            placed=False,
            broker_message=f"{e.kind}: {e}",
            plan_id=ctx.plan_id,
        )
        return ToolOutcome(content=f"broker refused: {e}", is_error=True)

    journal_order_attempt(
        side=side,
        lots=lots,
        entry=entry_price,
        stop=stop_price,
        take_profit=take_profit,
        mode="LIVE",
        placed=result.placed,
        broker_message=result.message,
        plan_id=ctx.plan_id,
    )
    if result.placed and result.order_id:
        journal_position_event(
            action="opened",
            position_id=result.order_id,
            side=side,
            lots=lots,
            entry=entry_price,
            plan_id=ctx.plan_id,
        )
    return ToolOutcome(
        content=(
            f"{'placed' if result.placed else 'NOT placed'}: "
            f"{side} {lots} lots — {result.message}"
        ),
        data={
            "placed": result.placed,
            "order_id": result.order_id,
            "message": result.message,
            "execution_mode": mode,
        },
    )


xauusd_place_order_tool = Tool(
    name="xauusd_place_order",
    description=(
        "Place an XAU/USD order on the attached Hugosway session. "
        "Four-layer gate: LIVE_TRADING_ENABLED constant, attached "
        "broker adapter, arg validation, execution_mode. Risk-class "
        "FINANCIAL — the Gateway queues for operator approval when "
        "execution_mode is 'approve'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "side": {"type": "string", "enum": ["LONG", "SHORT"]},
            "lots": {"type": "number", "exclusiveMinimum": 0},
            "order_type": {
                "type": "string",
                "enum": ["MARKET", "LIMIT", "STOP"],
            },
            "entry_price": {"type": "number", "exclusiveMinimum": 0},
            "stop_price": {"type": "number", "exclusiveMinimum": 0},
            "take_profit_price": {
                "type": "number",
                "exclusiveMinimum": 0,
            },
        },
        "required": ["side", "lots", "stop_price"],
    },
    risk=RiskClass.FINANCIAL,
    handler=_place_order,
)


# ── xauusd_flatten_all ───────────────────────────────────────────


async def _flatten_all(args: dict, ctx: ToolContext) -> ToolOutcome:
    reason = str(args.get("reason") or "manual flatten")
    journal_safety_interrupt(reason=reason, plan_id=ctx.plan_id)

    # Always force-disable the state machine on a flatten call — even
    # if LIVE_TRADING_ENABLED is False or no adapter is installed. Over-
    # applying DISABLED is always safer than under-applying.
    t = _STATE.force_disable(f"flatten_all: {reason}")
    journal_state(t, plan_id=ctx.plan_id)

    adapter = get_broker()
    results: list[dict[str, Any]] = []
    if adapter is not None and LIVE_TRADING_ENABLED:
        try:
            raw = await adapter.close_all_positions()
            for r in raw:
                results.append(
                    {
                        "order_id": r.order_id,
                        "placed": r.placed,
                        "message": r.message,
                    }
                )
                journal_position_event(
                    action="closed",
                    position_id=r.order_id,
                    plan_id=ctx.plan_id,
                )
        except BrokerError as e:
            return ToolOutcome(
                content=(
                    f"flatten_all: state DISABLED but broker "
                    f"close_all raised {e.kind}: {e}"
                ),
                is_error=True,
                data={"state": _STATE.current.value, "results": results},
            )

    return ToolOutcome(
        content=(
            f"flatten_all ok — {len(results)} position(s) closed, "
            "state machine DISABLED."
        ),
        data={"state": _STATE.current.value, "results": results},
    )


xauusd_flatten_all_tool = Tool(
    name="xauusd_flatten_all",
    description=(
        "Emergency stop: close every open XAU/USD position on the "
        "attached session and force the agent to DISABLED. Always "
        "allowed; no-op on broker if no adapter is attached. Safe to "
        "call from any state."
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


# ── xauusd_take_over (factory — needs BrowserSessionManager) ─────


def make_xauusd_take_over_tool(browser_sessions: Any) -> Tool:
    """Factory that binds the take-over tool to the live
    ``BrowserSessionManager``.

    The resulting tool expects a Browserbase session id the operator has
    already driven into a logged-in Hugosway trading page. The tool:

        1. Pulls the Playwright ``Page`` off the session manager.
        2. Instantiates a ``HugoswayAdapter`` around it.
        3. Calls ``verify_session`` to prove the page is on Hugosway,
           XAUUSD is selected, and account info is scrape-able.
        4. Installs the adapter as the process-wide broker and records
           the attached session.

    Risk-class FINANCIAL so the Gateway always queues an operator
    approval — the human is explicitly handing keys to the agent here
    regardless of execution_mode.
    """

    async def _take_over(args: dict, ctx: ToolContext) -> ToolOutcome:
        session_id = str(args.get("browser_session_id") or "").strip()
        if not session_id:
            return ToolOutcome(
                content="xauusd_take_over requires 'browser_session_id'.",
                is_error=True,
            )
        confirm = str(args.get("confirm") or "").strip().upper()
        if confirm != "TAKEOVER":
            return ToolOutcome(
                content=(
                    "xauusd_take_over requires confirm='TAKEOVER' "
                    "(verbatim). Refusing."
                ),
                is_error=True,
            )
        account_type = str(args.get("account_type") or "demo").lower()
        if account_type not in {"demo", "live"}:
            return ToolOutcome(
                content=(
                    f"account_type must be 'demo' or 'live', got '{account_type}'."
                ),
                is_error=True,
            )

        # Pull the Page the operator already drove into Hugosway.
        pages = getattr(browser_sessions, "_pages", {})
        page = pages.get(session_id)
        if page is None:
            return ToolOutcome(
                content=(
                    f"no Browserbase page for session '{session_id}'. "
                    "Open one and navigate to Hugosway first."
                ),
                is_error=True,
            )

        adapter = HugoswayAdapter(
            page=page,
            session_id=session_id,
            account_type=account_type,
        )
        try:
            info = await adapter.verify_session()
        except BrokerError as e:
            journal_safety_interrupt(
                reason=f"take_over verify failed: {e}",
                payload={"kind": e.kind, "session_id": session_id},
                plan_id=ctx.plan_id,
            )
            return ToolOutcome(
                content=f"verify failed: {e}", is_error=True
            )

        set_broker(adapter)
        attached = set_attached_session(
            session_id=session_id,
            account_type=account_type,
            account_id=info.account_id,
            note=str(args.get("note") or "").strip() or None,
        )
        journal_broker_event(
            action="take_over",
            session_id=session_id,
            account_type=account_type,
            details={
                "balance_usd": info.balance_usd,
                "leverage": info.leverage,
                "connected": info.connected,
            },
            plan_id=ctx.plan_id,
        )
        return ToolOutcome(
            content=(
                f"attached to {account_type} account "
                f"(balance=${info.balance_usd:.2f}, "
                f"leverage=1:{info.leverage})"
            ),
            data={
                "session_id": attached.session_id,
                "account_type": attached.account_type,
                "attached_at": attached.attached_at,
                "balance_usd": info.balance_usd,
                "leverage": info.leverage,
            },
        )

    return Tool(
        name="xauusd_take_over",
        description=(
            "Hand a Browserbase session (already logged into Hugosway "
            "and viewing XAUUSD) to the agent. Verifies the page is on "
            "Hugosway and scrapes account info as a smoke test before "
            "installing the adapter. Requires confirm='TAKEOVER' "
            "verbatim and account_type='demo'|'live'. Risk-class "
            "FINANCIAL so the operator must approve every attach."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "browser_session_id": {"type": "string"},
                "account_type": {
                    "type": "string",
                    "enum": ["demo", "live"],
                },
                "confirm": {
                    "type": "string",
                    "description": "Must be 'TAKEOVER' exactly.",
                },
                "note": {"type": "string"},
            },
            "required": ["browser_session_id", "account_type", "confirm"],
        },
        risk=RiskClass.FINANCIAL,
        handler=_take_over,
    )


XAUUSD_TOOLS: list[Tool] = [
    xauusd_evaluate_tool,
    xauusd_calc_size_tool,
    xauusd_state_tool,
    xauusd_get_candles_tool,
    xauusd_account_info_tool,
    xauusd_open_positions_tool,
    xauusd_release_tool,
    xauusd_place_order_tool,
    xauusd_flatten_all_tool,
]
"""Broker-independent XAUUSD tools.

``xauusd_take_over`` is session-bound and lives in
``make_xauusd_take_over_tool(browser_sessions)`` — registered by the
FastAPI lifespan, not here."""


def reset_state_for_tests() -> None:
    """Test helper — pytest only. Zeros every process-local singleton
    the tools share: state machine, attached broker, attached session."""
    global _STATE
    _STATE = StateMachine()
    set_broker(None)
    clear_attached_session()


__all__ = [
    "XAUUSD_TOOLS",
    "make_xauusd_take_over_tool",
    "reset_state_for_tests",
    "xauusd_account_info_tool",
    "xauusd_calc_size_tool",
    "xauusd_evaluate_tool",
    "xauusd_flatten_all_tool",
    "xauusd_get_candles_tool",
    "xauusd_open_positions_tool",
    "xauusd_place_order_tool",
    "xauusd_release_tool",
    "xauusd_state_tool",
]
