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

## Layer 6 — Forbidden UI actions (PR C, shipped)

Every click / fill through `HugoswayAdapter._safe_click` and
`_fill_by_label` runs through
`core.trading.xauusd.safety.forbidden_label_error` before touching the
DOM. The refusal list lives in two places (belt + suspenders):

- `XAUUSDConfig.forbidden_ui_labels` — operator-visible, editable in
  a follow-up PR: `withdraw / deposit / transfer / bank / card /
  payment / funding / wallet / cashier`.
- `broker.FORBIDDEN_EXACT_LABELS` — code-level floor, cannot be
  shrunk below the operator list: `Deposit / Withdraw / Withdrawals /
  Transfer / Bank / Card / Cashier / Funding / Wallet / Payment`.

Match is case-insensitive substring. On match the adapter raises
`BrokerError(kind="forbidden")`, which the tool layer logs as a
safety interrupt. No silent drop, no retry.

## Layer 7 — Attached-session runtime permission (PR C)

There is no "live trading enabled" runtime config. The runtime
permission model is:

1. Operator logs into Hugosway manually in a Browserbase live-view.
2. Operator calls `xauusd_take_over(browser_session_id=...,
   account_type='demo'|'live', confirm='TAKEOVER')`.
3. Adapter verifies the page is on Hugosway, XAUUSD is selected, and
   account info is scrape-able. Any failure refuses the attach.
4. Only with an attached session installed does `xauusd_place_order`
   reach the broker. All other execution tools also refuse.

Detach is `xauusd_release` — always allowed, forces `DISABLED`.

## Layer 8 — Approval gate (via PILK governor)

Every execution tool (`xauusd_take_over`, `xauusd_release`,
`xauusd_place_order`, `xauusd_flatten_all`) is tagged
`RiskClass.FINANCIAL`. PILK's approval gate requires explicit operator
approval for every call in `assistant` / `operator` autonomy profiles.

The execution_mode toggle (PR B) does **not** relax this for
`take_over` — handing the broker session to the agent is always a
manual operator decision. `autonomous` mode only skips approvals on
`place_order` *after* the session has been attached.

## Layer 9 — State machine invariants

- Every transition requires a non-empty `reason`.
- Illegal transitions raise `IllegalTransition` — the caller MUST
  handle it (we don't silently fall through).
- `DISABLED` is sticky: only operator-driven actions re-enter `OFF` or
  `SCANNING`. The agent cannot re-enable itself.
- `IN_POSITION → COOLDOWN` is the only exit from a live trade. No
  direct `IN_POSITION → READY_*` — forces a gap between trades.

## Layer 10 — Journaling = blameability

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
