"""XAUUSD execution runner — imperative 5-minute tick loop.

The agent manifest in ``agents/xauusd_execution_agent/manifest.yaml``
drives the LLM-style "reason about the next turn" flow the orchestrator
uses when the operator pokes at the agent manually. This module drives
the OTHER half — the operator says "start gold agent" on Telegram, and
from then on PILK owns a background asyncio task that:

1. Every 5 minutes: fetches 1M/5M/15M/1H/4H candles.
2. Calls :func:`core.trading.xauusd.evaluate_setup`.
3. If the verdict is TAKE_LONG / TAKE_SHORT:
      * Reads account equity via ``xauusd_account_info`` (broker).
      * Sizes the position via :func:`position_size_for_risk`.
      * Composes a rationale card for the operator, pushes it to
        Telegram, and queues the order via the approval gateway.
4. While a position is open: every 1 minute, reads open positions,
   shoves the P&L into Telegram so the operator sees live progress.
5. Every loop iteration: calls ``sentinel_heartbeat`` so the
   Sentinel supervisor can notice if the loop stalls.

### Execution mode

Default: ``approve`` — every order is queued for operator confirmation
via the approval gateway (which surfaces as a Telegram inline-button
card through the existing TelegramApprovals bridge). Switching to
``autonomous`` requires explicit operator action — there is NO code
path here that flips mode without that command. The operator says
"xauusd autonomous" or similar in chat, which calls the
``xauusd_state`` tool with execution_mode=autonomous.

### Safety

* The module-level ``LIVE_TRADING_ENABLED`` constant in
  :mod:`core.trading.xauusd.config` is still the last line of defence.
  When False, the runner goes through the evaluate → size → report
  path but ``xauusd_place_order`` refuses to talk to a real broker.
  Paper-mode journaling still produces artefacts the operator can
  review.
* ``NEWS_DISTORTED`` and other safety regimes auto-transition the
  state machine to DISABLED; the runner respects that and stops
  evaluating new setups until the operator clears the flag.
* If a tool fails 3 times in a row, the runner calls
  ``xauusd_flatten_all`` and transitions to DISABLED — we'd rather
  close flat and alert than keep running blind.

### Lifecycle

    runner = XAUUSDRunner(...)
    await runner.start()   # kicks off the 5-min loop + the 1-min P&L loop
    await runner.stop()    # cooperative shutdown; in-flight tick finishes

Constructed once per daemon (or once per "start gold agent" command);
``stop()`` is cooperative — any in-flight tool call finishes first,
then the loop exits on the next wait boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from core.integrations.telegram import TelegramClient, TelegramError
from core.logging import get_logger
from core.trading.xauusd import (
    AgentState,
    Candle,
    Evaluation,
    StateMachine,
    Verdict,
    XAUUSDConfig,
    evaluate_setup,
    position_size_for_risk,
)
from core.trading.xauusd.feed import FeedError, TwelveDataFeed

log = get_logger("pilkd.xauusd.runner")

# Tick cadence for the main decision loop. Gold's 5M candle is the
# primary timeframe the rule engine reasons against; anything faster
# just churns API budget.
DEFAULT_TICK_SECONDS = 5 * 60
# P&L update cadence while a position is open. One minute is enough
# to catch a stop or target fill without spamming the operator's
# Telegram history.
DEFAULT_PNL_TICK_SECONDS = 60
# Max consecutive tool failures before the runner flips DISABLED and
# escalates. Matches the existing Sentinel remediation retry budget —
# a blown call twice is a hiccup; three is a pattern.
MAX_TOOL_FAILURES = 3
# How long to wait for a single tool-like call before giving up. The
# feed + broker each enforce their own smaller timeouts; this is the
# coroutine-level backstop.
TOOL_CALL_TIMEOUT_S = 30.0


BrokerCall = Callable[..., Awaitable[dict[str, Any]]]
OrderPlacer = Callable[..., Awaitable[dict[str, Any]]]
HeartbeatFn = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class XAUUSDRunnerConfig:
    """Tunable knobs for one runner instance.

    Passed at construction rather than pulled from the settings module
    so tests can drop in arbitrary cadences without poking os.environ.
    """

    tick_seconds: float = DEFAULT_TICK_SECONDS
    pnl_tick_seconds: float = DEFAULT_PNL_TICK_SECONDS
    execution_mode: str = "approve"  # "approve" | "autonomous"
    trade_config: XAUUSDConfig = field(default_factory=XAUUSDConfig)
    # Candle counts per timeframe. Oldest → newest, deeper for the
    # higher timeframes so EMA50/200 have enough history.
    candle_counts: dict[str, int] = field(
        default_factory=lambda: {
            "1M": 250, "5M": 250, "15M": 300, "1H": 300, "4H": 500,
        }
    )


class XAUUSDRunner:
    """Background tick loop that runs the XAU/USD playbook.

    Instantiate once per daemon; call :meth:`start` on "start gold
    agent" and :meth:`stop` on "stop gold agent" (or daemon shutdown).
    """

    def __init__(
        self,
        *,
        feed: TwelveDataFeed,
        state: StateMachine,
        account_info_fn: BrokerCall,
        open_positions_fn: BrokerCall,
        place_order_fn: OrderPlacer,
        flatten_all_fn: BrokerCall,
        telegram_client: TelegramClient | None = None,
        heartbeat_fn: HeartbeatFn | None = None,
        config: XAUUSDRunnerConfig | None = None,
    ) -> None:
        self._feed = feed
        self._state = state
        self._account_info = account_info_fn
        self._open_positions = open_positions_fn
        self._place_order = place_order_fn
        self._flatten_all = flatten_all_fn
        self._telegram = telegram_client
        self._heartbeat = heartbeat_fn
        self._cfg = config or XAUUSDRunnerConfig()
        self._stop = asyncio.Event()
        self._tick_task: asyncio.Task | None = None
        self._pnl_task: asyncio.Task | None = None
        self._failures = 0

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._tick_task is not None:
            return
        self._stop.clear()
        self._failures = 0
        self._tick_task = asyncio.create_task(
            self._run(), name="xauusd-runner-tick"
        )
        self._pnl_task = asyncio.create_task(
            self._pnl_loop(), name="xauusd-runner-pnl"
        )
        await self._safe_telegram(
            "XAUUSD runner started — evaluating every 5 minutes, P&L "
            "every 1 minute while in position. "
            f"Execution mode: {self._cfg.execution_mode}."
        )
        log.info(
            "xauusd_runner_started",
            execution_mode=self._cfg.execution_mode,
            tick_s=self._cfg.tick_seconds,
        )

    async def stop(self) -> None:
        self._stop.set()
        for task in (self._tick_task, self._pnl_task):
            if task is None:
                continue
            try:
                await asyncio.wait_for(task, timeout=self._cfg.tick_seconds + 5)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._tick_task = None
        self._pnl_task = None
        await self._safe_telegram("XAUUSD runner stopped.")
        log.info("xauusd_runner_stopped")

    @property
    def running(self) -> bool:
        return self._tick_task is not None and not self._tick_task.done()

    # ── main 5-minute loop ───────────────────────────────────────

    async def _run(self) -> None:
        try:
            # Tick immediately at start so we don't wait 5 minutes for
            # the first evaluation after ``start gold agent``.
            await self._tick()
            while not self._stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._cfg.tick_seconds,
                    )
                if self._stop.is_set():
                    return
                await self._tick()
        except asyncio.CancelledError:
            return
        except Exception as e:  # pragma: no cover - defence in depth
            log.exception("xauusd_runner_crashed", error=str(e))
            await self._safe_telegram(
                f"XAUUSD runner crashed: {e}. Stopping — start again when safe."
            )

    async def _tick(self) -> None:
        await self._emit_heartbeat(
            status="ok",
            extra={"loop": "tick", "state": self._state.current.value},
        )
        if self._state.current is AgentState.DISABLED:
            # Respect safety trips. Operator clears them from the dashboard
            # or via a direct state transition.
            return
        try:
            candles = await self._fetch_all_candles()
        except FeedError as e:
            await self._report_tool_failure("fetch_candles", str(e))
            return
        except TimeoutError:
            await self._report_tool_failure("fetch_candles", "timeout")
            return

        evaluation: Evaluation
        try:
            evaluation = evaluate_setup(
                candles_1m=candles.get("1M", []),
                candles_5m=candles["5M"],
                candles_15m=candles["15M"],
                candles_1h=candles["1H"],
                candles_4h=candles["4H"],
                spread_usd=0.30,  # a reasonable default; broker can refine
                config=self._cfg.trade_config,
            )
        except Exception as e:
            await self._report_tool_failure("evaluate_setup", str(e))
            return
        # Reset the failure counter on a clean evaluation pass — the
        # runner is only "broken" if we can't get through this block.
        self._failures = 0

        verdict = evaluation.verdict
        if verdict is Verdict.DISABLED:
            self._safe_transition(
                AgentState.DISABLED,
                reason=evaluation.reason or "evaluator auto-disabled",
            )
            await self._safe_telegram(
                f"XAUUSD verdict: DISABLED — {evaluation.reason}."
            )
            return
        if verdict is Verdict.NO_TRADE:
            # Stay in SCANNING/WATCHLIST; log context so the operator
            # can see WHY PILK passed.
            log.info(
                "xauusd_no_trade",
                reason=evaluation.reason,
                regime=getattr(evaluation, "regime", None),
            )
            return

        await self._plan_and_place(verdict, evaluation, candles)

    async def _plan_and_place(
        self,
        verdict: Verdict,
        evaluation: Evaluation,
        candles: dict[str, list[Candle]],
    ) -> None:
        # Pull broker equity before sizing so the risk layer gets a
        # real number and not a cached guess.
        try:
            account = await asyncio.wait_for(
                self._account_info(), timeout=TOOL_CALL_TIMEOUT_S,
            )
        except Exception as e:
            await self._report_tool_failure("account_info", str(e))
            return
        equity = float(account.get("equity_usd") or account.get("equity") or 0.0)
        if equity <= 0:
            await self._safe_telegram(
                "XAUUSD: broker returned zero equity — refusing to size."
            )
            return

        entry = float(candles["5M"][-1].close)
        # Pull the stop / direction off the evaluation if the rule
        # engine provided them; otherwise fall back to a fixed ATR-
        # distance stop so the risk layer has something to reason with.
        stop_price = getattr(evaluation, "stop_price", None)
        if stop_price is None:
            # 3$ stop is inside the min/max stop bracket and gives the
            # risk layer a sane default until the rule engine emits
            # an explicit stop. This path should be rare — it only
            # fires when the evaluator greenlit a trade but omitted
            # the stop, which is a bug worth seeing in logs.
            stop_price = (
                entry - 3.0 if verdict is Verdict.TAKE_LONG else entry + 3.0
            )
            log.warning(
                "xauusd_runner_missing_stop",
                verdict=verdict.value,
                entry=entry,
                derived_stop=stop_price,
            )

        sizing = position_size_for_risk(
            equity_usd=equity,
            entry_price=entry,
            stop_price=float(stop_price),
            spread_usd=0.30,
            config=self._cfg.trade_config,
        )
        if getattr(sizing, "is_refusal", False) or not getattr(
            sizing, "lots", 0,
        ):
            reason = getattr(sizing, "reason", "risk layer refused")
            await self._safe_telegram(
                f"XAUUSD sizing refused: {reason}. Staying in SCANNING."
            )
            return

        direction = "LONG" if verdict is Verdict.TAKE_LONG else "SHORT"
        rationale = _compose_rationale(
            direction=direction,
            entry=entry,
            stop=float(stop_price),
            lots=float(sizing.lots),
            equity=equity,
            evaluation=evaluation,
            mode=self._cfg.execution_mode,
        )
        await self._safe_telegram(rationale)

        # Transition through BIASED_* → READY_* → IN_POSITION. The
        # state machine rejects illegal transitions; we only move
        # forward when the previous edge is legal.
        ready_state = (
            AgentState.READY_LONG if direction == "LONG"
            else AgentState.READY_SHORT
        )
        biased_state = (
            AgentState.BIASED_LONG if direction == "LONG"
            else AgentState.BIASED_SHORT
        )
        self._safe_transition(biased_state, reason=f"verdict {verdict.value}")
        self._safe_transition(ready_state, reason="setup confirmed on 5M")

        # The place_order function is itself approval-gated in
        # "approve" mode. We await its decision before transitioning
        # IN_POSITION so a rejected approval leaves the state at
        # READY_* and the next tick can re-evaluate.
        try:
            result = await asyncio.wait_for(
                self._place_order(
                    direction=direction,
                    lots=float(sizing.lots),
                    entry_price=entry,
                    stop_price=float(stop_price),
                    mode=self._cfg.execution_mode,
                ),
                timeout=TOOL_CALL_TIMEOUT_S,
            )
        except Exception as e:
            await self._report_tool_failure("place_order", str(e))
            return

        if result.get("placed"):
            self._safe_transition(
                AgentState.IN_POSITION,
                reason=f"order placed id={result.get('order_id', '?')}",
            )
            await self._safe_telegram(
                f"XAUUSD {direction} placed: {sizing.lots} lots @ {entry:.2f}, "
                f"stop {stop_price:.2f}. Monitoring every minute."
            )
        else:
            # The approval was declined or the broker refused; the
            # gateway / broker already has the reason in the result.
            reason = result.get("reason") or "declined"
            await self._safe_telegram(
                f"XAUUSD order NOT placed — {reason}. Back to SCANNING."
            )
            self._safe_transition(AgentState.SCANNING, reason=f"order declined: {reason}")

    # ── P&L loop (fires only when we're in position) ─────────────

    async def _pnl_loop(self) -> None:
        try:
            while not self._stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self._cfg.pnl_tick_seconds,
                    )
                if self._stop.is_set():
                    return
                await self._emit_heartbeat(
                    status="ok",
                    extra={"loop": "pnl", "state": self._state.current.value},
                )
                if self._state.current is not AgentState.IN_POSITION:
                    continue
                try:
                    positions = await asyncio.wait_for(
                        self._open_positions(),
                        timeout=TOOL_CALL_TIMEOUT_S,
                    )
                except Exception as e:
                    log.warning("xauusd_pnl_fetch_failed", error=str(e))
                    continue
                summary = _summarize_positions(positions)
                if summary is None:
                    # No open positions reported — transition back to
                    # SCANNING so the next evaluation can fire.
                    self._safe_transition(
                        AgentState.COOLDOWN,
                        reason="no open positions — broker reports flat",
                    )
                    await self._safe_telegram(
                        "XAUUSD position closed. Cooling down before next setup."
                    )
                    continue
                await self._safe_telegram(summary)
        except asyncio.CancelledError:
            return
        except Exception as e:  # pragma: no cover - defence
            log.exception("xauusd_pnl_loop_crashed", error=str(e))

    # ── helpers ──────────────────────────────────────────────────

    async def _fetch_all_candles(
        self,
    ) -> dict[str, list[Candle]]:
        """Pull every timeframe in parallel. Failures on any single
        timeframe abort the whole tick — the evaluator can't reason
        on half a picture, and the broker + feed both rate-limit."""
        results: dict[str, list[Candle]] = {}
        tasks = {
            tf: asyncio.create_task(
                asyncio.wait_for(
                    self._feed.fetch_candles(tf, count),
                    timeout=TOOL_CALL_TIMEOUT_S,
                ),
                name=f"xauusd-feed-{tf}",
            )
            for tf, count in self._cfg.candle_counts.items()
        }
        try:
            for tf, task in tasks.items():
                res = await task
                results[tf] = list(res.candles)
        finally:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
        return results

    async def _report_tool_failure(self, op: str, detail: str) -> None:
        self._failures += 1
        log.warning(
            "xauusd_runner_tool_failed",
            op=op,
            detail=detail,
            failures=self._failures,
        )
        await self._emit_heartbeat(
            status="warning",
            extra={"op": op, "failures": self._failures},
        )
        if self._failures >= MAX_TOOL_FAILURES:
            # Flatten + disable. We'd rather be safe than silent.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._flatten_all(reason=f"runner: {op} failed {self._failures}x"),
                    timeout=TOOL_CALL_TIMEOUT_S,
                )
            self._safe_transition(
                AgentState.DISABLED,
                reason=f"runner tool failures: {op}",
            )
            await self._safe_telegram(
                f"XAUUSD runner disabled after {self._failures} consecutive "
                f"failures on {op}: {detail}. Flattened all positions."
            )
            self._stop.set()

    def _safe_transition(self, target: AgentState, *, reason: str) -> None:
        """Transition when the table allows it, log + ignore when it
        doesn't. The state machine's ``transition`` raises on illegal
        edges; we translate that into a warning instead of propagating
        because the caller is always "the runner is in-line with the
        rulebook" not "blow up the process"."""
        try:
            self._state.transition(target, reason=reason)
        except Exception as e:
            log.warning(
                "xauusd_runner_illegal_transition",
                from_state=self._state.current.value,
                to_state=target.value,
                reason=reason,
                error=str(e),
            )

    async def _emit_heartbeat(
        self, *, status: str, extra: dict[str, Any],
    ) -> None:
        if self._heartbeat is None:
            return
        try:
            await self._heartbeat(status, extra)
        except Exception as e:
            log.warning("xauusd_runner_heartbeat_failed", error=str(e))

    async def _safe_telegram(self, text: str) -> None:
        if self._telegram is None:
            return
        try:
            await self._telegram.send_message(text)
        except TelegramError as e:
            log.warning(
                "xauusd_runner_telegram_failed",
                status=e.status,
                message=e.message,
            )
        except Exception as e:
            log.warning("xauusd_runner_telegram_error", error=str(e))


# ── formatting helpers ───────────────────────────────────────────


def _compose_rationale(
    *,
    direction: str,
    entry: float,
    stop: float,
    lots: float,
    equity: float,
    evaluation: Evaluation,
    mode: str,
) -> str:
    """Short, scannable setup card for Telegram.

    Used before queuing the order so the operator reads the WHY
    (bias, regime, RSI/ADX summary if available) before the WHAT
    (size + entry + stop) and finally the approval ask.
    """
    bias = getattr(evaluation, "bias", None)
    regime = getattr(evaluation, "regime", None)
    reason = getattr(evaluation, "reason", "")
    risk_usd = abs(entry - stop) * lots * 100  # XAU lots x 100 oz
    lines = [
        f"XAUUSD setup — {direction}",
        f"Entry: {entry:.2f}   Stop: {stop:.2f}   Size: {lots:.2f} lots",
        f"Risk: ~${risk_usd:.2f} on ${equity:.2f} equity",
    ]
    if bias is not None:
        lines.append(f"Bias: {bias}")
    if regime is not None:
        lines.append(f"Regime: {regime}")
    if reason:
        lines.append(f"Reason: {reason}")
    if mode == "approve":
        lines.append(
            "Queuing for approval — tap ✅ in the approval card to confirm."
        )
    else:
        lines.append("Autonomous mode — placing now.")
    return "\n".join(lines)


def _summarize_positions(positions: Any) -> str | None:
    """Compress the broker's positions payload into one line.

    Returns None when the broker reports no open positions — the
    caller treats that as "we're flat, transition back".
    """
    rows: list[dict[str, Any]]
    if isinstance(positions, dict):
        rows = list(positions.get("positions") or [])
    elif isinstance(positions, list):
        rows = list(positions)
    else:
        rows = []
    if not rows:
        return None
    parts = []
    for p in rows[:3]:  # cap at 3 — XAUUSD rarely needs more
        side = p.get("side") or p.get("direction") or "?"
        lots = p.get("lots") or p.get("size") or 0
        pnl = p.get("pnl_usd") or p.get("pnl") or 0
        entry = p.get("entry_price") or p.get("entry") or 0
        parts.append(
            f"{side} {lots} lots @ {float(entry):.2f} · P&L ${float(pnl):.2f}"
        )
    return "XAUUSD live positions — " + " · ".join(parts) + (
        f" (+{len(rows) - 3} more)" if len(rows) > 3 else ""
    )


__all__ = [
    "DEFAULT_PNL_TICK_SECONDS",
    "DEFAULT_TICK_SECONDS",
    "MAX_TOOL_FAILURES",
    "TOOL_CALL_TIMEOUT_S",
    "XAUUSDRunner",
    "XAUUSDRunnerConfig",
]
