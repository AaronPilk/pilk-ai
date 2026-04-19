# Live-Readiness Checklist

**Going live is a deliberate, reviewable event. No one item on this
list may be skipped.** If you're reviewing a PR that flips
`LIVE_TRADING_ENABLED = True`, every checkbox below must be ticked,
evidence attached, and signed off by someone other than the author.

## Pre-flight (code)

- [ ] `LIVE_TRADING_ENABLED = False` today. Flipping to `True` is a
      single-file edit in `core/trading/xauusd/config.py`, reviewed
      alongside the broker-adapter PR — never in isolation.
- [ ] Broker adapter (Hugosway via Browserbase, PR C) is merged and
      its tests cover: successful order, rejected order, order that
      never confirms (timeout path), broker UI change detection.
- [ ] Price-feed adapter (Twelve Data, PR B) is merged. Feed latency
      is below 2 seconds per timeframe fetch in normal conditions.
- [ ] `xauusd_place_order` and `xauusd_flatten_all` are still
      `RiskClass.FINANCIAL` and the approval gate still fires.
- [ ] `forbidden_ui_labels` is current (audit the Hugosway UI for any
      added funding-related labels).
- [ ] Stop-loss is attached on every live order. The broker adapter
      refuses orders without a confirmed stop.
- [ ] `xauusd_state` force-disables on any broker-adapter exception.

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
