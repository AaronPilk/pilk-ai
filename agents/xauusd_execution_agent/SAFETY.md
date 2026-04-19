# xauusd_execution_agent — Safety Guardrails

Every guardrail here is a hard rule, not a suggestion. The agent's
architecture is built so that **any one of these failing closes the
trade.** A trade happens only when every gate agrees.

## Layer 0 — Python-level hard gate

`core/trading/xauusd/config.py`:

```python
LIVE_TRADING_ENABLED: bool = False
```

- Module-level constant. Not a config field, not an env var.
- Every live-order codepath checks this. When False, order placement
  refuses unconditionally and journals a safety interrupt.
- Flipping to `True` is a deliberate code edit that **must** ride
  alongside a fully tested broker adapter in the same PR.

## Layer 1 — Symbol whitelist

`ALLOWED_SYMBOLS = {"XAUUSD", "XAU/USD", "GOLD"}`.

Every tool that takes a `symbol` argument calls `_enforce_symbol`
before doing anything else. A mismatch returns `is_error=True` and
logs a safety interrupt. The agent cannot be tricked (via prompt
injection or chain-of-thought drift) into trading another instrument.

## Layer 2 — Rule engine refusal-by-default

`evaluate_setup` has 9 gates. Each returns `NO_TRADE` or `DISABLED`
unless every prior gate passed. Defaults fail closed:

- Insufficient history → `NO_TRADE`.
- Unknown regime → `NO_TRADE`.
- `NEWS_DISTORTED` → `DISABLED` (auto-disable).
- Warm-up incomplete on any indicator → `NO_TRADE`.

No "best-effort" fallback. No rule ever gets "I'll try anyway."

## Layer 3 — Risk engine refusal-as-return-value

`position_size_for_risk` returns either a `PositionSize` or a
`SizingRefusal`. Callers MUST check which one they got. If it's a
refusal:

- Stop-distance too tight / too wide → refuse.
- Spread too wide → refuse.
- Target risk below broker's min-lot → refuse.
- Margin cap hit → refuse.

The agent never "picks a smaller size" to force a fill — that's
exactly the behavior gold chewed up retail traders with.

## Layer 4 — Budget caps

| Cap | Default | What happens |
|---|---|---|
| `max_risk_per_trade_pct` | 0.5% | enforced at size time |
| `max_daily_loss_pct` | 3% | `apply_daily_loss_gate` auto-disables |
| `max_equity_drawdown_pct` | 10% | `apply_drawdown_gate` auto-disables |
| `max_margin_usage_pct` | 25% | enforced at size time |
| `min_account_balance_to_continue` | $250 | refuse all sizing below |
| `max_open_trades` | 3 | orchestrator enforces |

"Auto-disable" means the state machine force-moves to `DISABLED`.
The agent cannot re-enable itself. Re-enable is an operator action
on the dashboard (PR C).

## Layer 5 — Anomaly auto-disable

`classify_regime` labels the tape `NEWS_DISTORTED` when any recent
5-candle window shows a single-candle range ≥ `anomaly_tick_jump_usd`
(default $12). The rule engine returns `DISABLED` immediately. The
agent must transition to `DISABLED`; the next-candle loop will see
DISABLED and skip evaluation until the operator re-enables.

## Layer 6 — Forbidden UI actions (future, PR C)

When the Hugosway Browserbase adapter ships, every click/fill action
against the broker UI will be cross-checked against
`forbidden_ui_labels`:

```
("withdraw", "deposit", "transfer", "bank", "card",
 "payment", "funding", "wallet", "cashier")
```

If a selector matches any of these (case-insensitive), the adapter
refuses the action, journals a safety interrupt, and force-disables
the agent. Defends against prompt-injected plan steps trying to move
money outside of trading.

## Layer 7 — Approval gate (via PILK governor)

The two execution tools (`xauusd_place_order`, `xauusd_flatten_all`)
are tagged `RiskClass.FINANCIAL`. PILK's approval gate requires
explicit operator approval for every call in `assistant` / `operator`
autonomy profiles. Even when the Python hard gate flips, the
approval gate still fires until the operator grants a trust rule.

## Layer 8 — State machine invariants

- Every transition requires a non-empty `reason`.
- Illegal transitions raise `IllegalTransition` — the caller MUST
  handle it (we don't silently fall through).
- `DISABLED` is sticky: only operator-driven actions re-enter `OFF` or
  `SCANNING`. The agent cannot re-enable itself.
- `IN_POSITION → COOLDOWN` is the only exit from a live trade. No
  direct `IN_POSITION → READY_*` — forces a gap between trades.

## Layer 9 — Journaling = blameability

Every decision is logged with:
- event class (`xauusd.state | .verdict | .risk | .order | .safety`)
- reason (always present)
- plan_id (to tie back to the orchestrator turn)

Post-mortems are `grep xauusd.safety` + sort by time. If a live trade
ever misbehaves, the full chain of reasoning is on disk.

## What this agent will NEVER do

- Trade any symbol other than XAU/USD.
- Place an order without a stop-loss price.
- Increase size after a loss.
- "Scale into" a losing trade.
- Trade during `NEWS_DISTORTED` regime.
- Override the state machine's transition table.
- Click "withdraw" / "deposit" / "transfer" / etc. on the broker UI.
- Re-enable itself after a safety trip.
