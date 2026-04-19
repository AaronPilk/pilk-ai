# Live-Readiness Checklist

**Going live is a deliberate, reviewable event. No one item on this
list may be skipped.** If you're reviewing a PR that flips
`LIVE_TRADING_ENABLED = True`, every checkbox below must be ticked,
evidence attached, and signed off by someone other than the author.

## Pre-flight (code)

- [ ] `LIVE_TRADING_ENABLED = False` today. Flipping to `True` is a
      single-file edit in `core/trading/xauusd/config.py`, reviewed
      as its own PR titled `ENABLE LIVE TRADING` with evidence from
      the smoke-test run attached.
- [x] Broker adapter plumbing (PR C) merged: `BrokerAdapter` protocol,
      `MockBroker`, `HugoswayAdapter` skeleton, `xauusd_take_over` /
      `xauusd_release` / `xauusd_account_info` / `xauusd_open_positions`
      tools, four-gate `place_order`.
- [ ] `HugoswayAdapter` selectors verified against live Hugosway DOM.
      Every `NEEDS_LIVE_VERIFY` comment in `core/trading/xauusd/broker.py`
      either resolved (comment removed) or replaced with a justified
      current selector.
- [ ] `HugoswayAdapter.close_position` and `close_all_positions` are
      implemented (currently raise `BrokerError`).
- [x] Price-feed adapter (Twelve Data, PR B) is merged.
- [ ] Feed latency is below 2 seconds per timeframe fetch in normal
      conditions — measured, not assumed.
- [x] `xauusd_place_order`, `xauusd_flatten_all`, `xauusd_take_over`,
      `xauusd_release` are all `RiskClass.FINANCIAL` and the approval
      gate still fires even in `autonomous` execution_mode for
      `take_over` (the operator's explicit hand-off).
- [x] `forbidden_ui_labels` + the adapter's `FORBIDDEN_EXACT_LABELS`
      list cover deposit/withdraw/transfer/bank/card/payment/cashier/
      funding/wallet — every click and fill runs through
      `forbidden_label_error` before touching the DOM.
- [ ] Stop-loss is attached on every live order. The adapter refuses
      orders that arrive without `stop_loss_price`.
- [x] `xauusd_flatten_all` always force-disables regardless of adapter
      state or `LIVE_TRADING_ENABLED`.

## Smoke-test gate (runs once before the flip)

- [ ] Operator logs into a **demo** Hugosway account in a Browserbase
      live-view. Balance ≥ $100, leverage set matches config.
- [ ] `xauusd_take_over(browser_session_id=..., account_type='demo',
      confirm='TAKEOVER')` attaches cleanly; balance/leverage match
      what's visible in the browser.
- [ ] `xauusd_account_info` reads balance / equity / free-margin
      identical to the bottom strip.
- [ ] `xauusd_open_positions` on a fresh demo returns `[]`.
- [ ] Manual trade test: flip `LIVE_TRADING_ENABLED` to `True` in a
      short-lived local branch, run a single 0.01-lot MARKET order
      through `xauusd_place_order` end-to-end, verify the position
      appears both in Hugosway Positions and in `xauusd_open_positions`.
- [ ] `xauusd_release(reason='smoke test done')` detaches; state
      forces to `DISABLED`.

## Pre-flight (data)

- [ ] Backtest over ≥6 months of XAU/USD 5M data. Win rate, average
      R, max drawdown documented.
- [ ] Forward test in paper mode for ≥2 full trading weeks. Journal
      shows state transitions match expectations.
- [ ] Weekly news-week run included in the forward test (NFP, FOMC,
      CPI) — verify `NEWS_DISTORTED` triggers and the agent
      correctly sits out.
- [ ] Spread + slippage caps calibrated to observed Hugosway
      behavior. `max_spread_usd` reflects the broker's typical
      spread plus a safe buffer.

## Pre-flight (account)

- [ ] Hugosway demo account used for the first 50 live-adapter
      trades. No real money until that batch looks clean.
- [ ] Separate small production account funded with an amount the
      operator is willing to lose entirely. Not the main trading
      stack.
- [ ] `max_daily_loss_pct` and `max_equity_drawdown_pct` set to
      halve the operator's stop-out thresholds, not match them.
- [ ] Withdraw-only wallet / crypto destination is pre-configured
      and tested. The agent never needs to initiate a withdrawal.

## Pre-flight (operational)

- [ ] Alerting: operator gets a push/slack notification on every
      state transition to `DISABLED`.
- [ ] Dashboard's "kill switch" button wired — one click →
      `xauusd_flatten_all(reason="operator kill")`.
- [ ] Weekly journal review: read the last 7 days of `xauusd.safety`
      events. Any repeats → investigate before next week's trading.
- [ ] Budget in PILK governor's daily cap set below the largest
      possible single-trade loss the risk engine would accept.

## The flip itself

- [ ] PR title clearly says `ENABLE LIVE TRADING` and is NOT a draft.
- [ ] PR description includes: which broker, which account, initial
      funding, daily cap, kill-switch evidence.
- [ ] Two-person review: the author + one other human read the full
      diff and sign off.
- [ ] First trading day = monitored live by the operator. No
      unattended runs.

## Post-flight rollback

- [ ] Revert plan documented. Flipping back to `False` is a
      one-line revert; know which PR is the rollback before the
      first live trade fires.
- [ ] DISABLED state is respected across restarts (PR C persists
      state machine to SQLite). Verify by killing pilkd mid-disabled
      and restarting.

---

**If any item is skipped or approximated: do not go live.** Gold
doesn't forgive shortcuts.
