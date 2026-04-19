# xauusd_execution_agent — Architecture

## Design goals (in priority order)

1. **Capital preservation.** Every gate fails closed. Refusal is the
   first-class outcome; a trade is the exception.
2. **Determinism.** Same inputs produce the same verdict. The rule
   engine is a pure function so backtests, unit tests, and live runs
   all agree on what "valid setup" means.
3. **Auditability.** Every state transition, verdict, sizing decision,
   and order attempt is journaled with a reason and a plan-id.
4. **Single-responsibility modules.** No cross-concerns. The rule
   engine never knows about orders; the risk engine never knows about
   indicators; the state machine never knows about the broker.
5. **Boring stack.** Pure Python, no pandas/numpy dependency, no TA
   library. Everything readable in one sitting.

## Package layout

```
core/trading/xauusd/
    __init__.py       — public API surface
    config.py         — immutable XAUUSDConfig + LIVE_TRADING_ENABLED
    candle.py         — Candle dataclass + helpers
    indicators.py     — EMA, RSI (Wilder), ADX (Wilder), slope
    structure.py      — pivots, HH/HL structure, regime classifier
    rules.py          — evaluate_setup() — the one decision function
    risk.py           — position_size_for_risk + daily/drawdown gates
    state.py          — AgentState enum + StateMachine + pinned table
    journal.py        — structured structlog helpers

core/tools/builtin/xauusd.py
    Thin async wrappers exposing the pure-Python primitives to the
    LLM, plus the two placeholder broker tools (place_order,
    flatten_all) that refuse until the adapter PR ships.

agents/xauusd_execution_agent/
    manifest.yaml     — system prompt + tool allowlist
    README.md
    ARCHITECTURE.md          (this file)
    SAFETY.md
    LIVE_READINESS_CHECKLIST.md
```

## Data flow (paper mode, today)

```
operator → chat message → agent
agent:
  1. xauusd_state  (get)            → state=SCANNING
  2. xauusd_evaluate (candles_*)    → verdict, details
  3. xauusd_state  (transition)     → BIASED_LONG
  4. xauusd_calc_size (equity,...)  → lots or refusal
  5. xauusd_state  (transition)     → READY_LONG
  6. xauusd_place_order             → REFUSED (paper)
     journal(order_attempt, placed=False, mode=PAPER)
  7. xauusd_state  (transition)     → BIASED_LONG | SCANNING
```

Every step is a tool call; every tool call is gated by the usual PILK
governor + approval flow.

## Data flow (live, post-PR-C)

Only difference: step 6 drives Hugosway via Browserbase, waits for a
broker confirmation, and on success step 7 transitions to IN_POSITION.
If the broker returns anything unexpected (timeout, anti-bot wall,
unexpected dialog), the adapter calls `xauusd_flatten_all` and lands
the agent in DISABLED.

## Multi-timeframe model

```
4H  — macro trend + major levels. NEUTRAL/LONG/SHORT bias.
 ↓
1H  — intermediate trend health. Confirms or weakens 4H.
 ↓
15M — session bias, structure break/retest.
 ↓
 5M — PRIMARY EXECUTION CHART. All setups valid here.
 ↓
 1M — entry refinement only. Never creates a trade thesis.
```

Bias for each TF is a single EMA-based snapshot (close vs fast-EMA +
slope). `_mtf_alignment` requires the 15M/1H/4H biases to be
supportive or neutral for a 5M direction to be accepted, unless
`allow_countertrend=True` in config.

## Rule engine gate order

`rules.evaluate_setup()` runs these gates top-to-bottom. The **first**
gate that returns a non-positive verdict short-circuits — no
downstream gate gets to overrule an earlier refusal.

1. **Sanity** — enough 5M history to warm up EMA + ADX.
2. **Spread** — `spread_usd ≤ max_spread_usd`.
3. **Indicators** — compute EMA fast/slow, RSI, ADX on 5M.
4. **Structure + regime** — pivots → HH/HL label → regime.
   - `NEWS_DISTORTED` → `DISABLED`.
   - `CHOP` / `RANGE` → `NO_TRADE`.
5. **5M trend** — price-above-EMA stack + positive slope for longs
   (mirror for shorts). Structure must not oppose.
6. **ADX strength** — `adx_value ≥ adx_min_trend`.
7. **RSI support** — long-side `rsi ≥ rsi_long_support_min`
   (short-side mirror).
8. **Candle confirmation** — body/range ≥ 0.5, engulfing or
   strong-impulse continuation.
9. **MTF alignment** — 15M/1H/4H biases supportive or neutral.
10. → `TAKE_LONG` / `TAKE_SHORT` with full details.

## Risk engine

`position_size_for_risk` is a single function taking config + equity +
entry + stop + spread. It applies these in order:

1. Equity floor (`min_account_balance_to_continue`).
2. Spread cap (`max_spread_usd`).
3. Stop-distance bounds (`min_stop_usd` ≤ distance ≤ `max_stop_usd`).
4. Target risk = `equity × max_risk_per_trade_pct%`.
5. Raw lots = target risk ÷ (stop × $100/lot/$move).
6. Floor to `lot_step` (default 0.01). Reject if below `min_lot`.
7. Margin = notional ÷ leverage. Reject if margin > cap.
8. Return `PositionSize` with realized risk, lots, margin, notional.
   Refuse with a specific reason on any failure.

Daily-loss and drawdown gates are separate pure functions
(`apply_daily_loss_gate`, `apply_drawdown_gate`) that the
orchestrator consults before every decision tick.

## State machine

10 states, pinned transition table. Highlights:

- **Every state can always move to DISABLED** via `force_disable`.
  That's the only bypass of the transition table and is used by safety
  interrupts.
- **DISABLED only un-sticks to OFF or SCANNING**, and only via the
  dashboard's "re-enable" action (PR C).
- **IN_POSITION only moves to COOLDOWN or DISABLED.** It cannot
  shortcut to another READY_* — you always cool off between trades.
- Every transition requires a non-empty `reason`.

## Journaling

Five event classes (all at INFO except safety at WARNING):

| Event | When |
|---|---|
| `xauusd.state` | every state transition |
| `xauusd.verdict` | every `xauusd_evaluate` call |
| `xauusd.risk` | every `xauusd_calc_size` call |
| `xauusd.order` | every order attempt (paper or live) |
| `xauusd.safety` | every force-disable or anomaly trip |

Structured fields means post-mortems are a few ripgrep queries, not a
treasure hunt. Ledger persistence lands in PR C alongside the broker
adapter — it's cheap to add once there's a real fill to tie to.

## What's deliberately *not* here

- No cross-symbol correlation logic (irrelevant; only XAU/USD).
- No portfolio allocation (single-instrument).
- No ML / statistical model (the rules are the model).
- No "recover losses by increasing size" behavior — forbidden.
- No on-disk state persistence beyond the Ledger (see Phase 2).
