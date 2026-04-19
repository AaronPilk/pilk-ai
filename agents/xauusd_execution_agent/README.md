# xauusd_execution_agent

Single-instrument XAU/USD trading agent. Top-down multi-timeframe
analysis, strict entry filters, hard risk caps, explicit state
machine, ruthless invalidation.

> **Paper-mode only in this release.** Every order-placement codepath
> is hard-gated off. See [SAFETY.md](SAFETY.md) and
> [LIVE_READINESS_CHECKLIST.md](LIVE_READINESS_CHECKLIST.md).

## What it trades

**XAU/USD only.** All other symbols are refused at the tool layer.
`EURUSD`, `GBPUSD`, `USDJPY`, indices, crypto, stocks — off-limits.

## What it does not trade (even if asked)

- Any symbol other than XAU/USD.
- Any setup outside its rule envelope (weak-body candles, flat EMAs,
  mid-range impulses, news-distorted ticks).
- Anything after a safety trip — DISABLED is sticky until the operator
  re-enables from the dashboard.

## How it works

One PILK agent definition + one package (`core/trading/xauusd/`) + one
tool surface (`core/tools/builtin/xauusd.py`).

- **Rule engine** (`rules.py`) — pure function. Given candle arrays for
  5M/1M/15M/1H/4H + current spread, returns one of:
  `TAKE_LONG`, `TAKE_SHORT`, `NO_TRADE`, or `DISABLED`, with a
  concrete reason.
- **Indicators** (`indicators.py`) — hand-rolled EMA, Wilder's RSI, and
  Wilder's ADX. No external TA library. Deterministic.
- **Structure** (`structure.py`) — pivot detection + HH/HL vs LL/LH
  labelling + regime classification (`TRENDING_*`, `PULLBACK`,
  `RANGE`, `CHOP`, `NEWS_DISTORTED`, etc.).
- **Risk** (`risk.py`) — position sizing from `(equity, entry, stop,
  spread)` with refusal-as-first-class-return. Never falls back to a
  smaller size to get filled.
- **State** (`state.py`) — 10-state machine with a pinned transition
  table. Illegal transitions raise. `force_disable` is the only
  bypass and is always allowed.
- **Journal** (`journal.py`) — every decision lands in structlog with a
  consistent schema (`xauusd.state`, `xauusd.verdict`,
  `xauusd.risk`, `xauusd.order`, `xauusd.safety`).

## Architecture

Short version: see [ARCHITECTURE.md](ARCHITECTURE.md).

## What ships in this PR

Pure-Python engine + agent manifest + tools + tests. **Zero network.**
Every external dependency is explicit and deferred:

| Concern | This PR | Follow-up |
|---|---|---|
| Rule engine | ✅ done, fully tested | — |
| Risk engine | ✅ done, fully tested | — |
| State machine | ✅ done, fully tested | — |
| Indicators | ✅ hand-rolled, golden values | — |
| Journaling | ✅ structlog | add Ledger rows in PR C |
| **Price feed** | ❌ placeholder tool | **PR B** (Twelve Data) |
| **Broker adapter** | ❌ placeholder tool | **PR C** (Hugosway via Browserbase) |
| **Live trading** | 🔒 hard-off in `config.py` | Manual code edit + PR review |

## Configuration

All knobs live in `core/trading/xauusd/config.py`. Defaults are
deliberately conservative for XAU/USD at 1:300 leverage. The agent's
manifest `policy.budget` also caps LLM spend per run.

| Knob | Default | Notes |
|---|---|---|
| `max_risk_per_trade_pct` | 0.5 | of equity |
| `max_daily_loss_pct` | 3.0 | auto-disable |
| `max_equity_drawdown_pct` | 10.0 | auto-disable |
| `max_margin_usage_pct` | 25.0 | hard cap |
| `max_spread_usd` | 0.50 | refuse above |
| `min_stop_usd` / `max_stop_usd` | 2.00 / 8.00 | XAU/USD-specific |
| `anomaly_tick_jump_usd` | 12.00 | news filter |
| `max_open_trades` | 3 | — |

## Going live — the hard gate

See [LIVE_READINESS_CHECKLIST.md](LIVE_READINESS_CHECKLIST.md). In
short: no UI toggle, no env var, no runtime flag can enable live
trading. The only path is a deliberate code edit to
`LIVE_TRADING_ENABLED = True` in `config.py`, reviewed alongside a
tested broker adapter.

## Example operator command (paper)

> "XAU/USD agent: analyze the current 5M setup. I'll paste candles
> below. Evaluate and tell me whether you'd take the trade, with full
> reasoning."

The agent will call `xauusd_evaluate` on the payload, transition its
state, and return the verdict — but `xauusd_place_order` will refuse
every time until the live gate flips.
