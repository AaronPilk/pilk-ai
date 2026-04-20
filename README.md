# PILK

**Local-first execution OS for agents.** PILK is a personal orchestrator that plans, routes, and supervises work across specialist agents running inside scoped sandboxes — cost-aware, approval-gated, single-machine by default.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the non-negotiables, the layered model, and the phased build plan.

## What's here today

50 merged PRs worth of system. The scaffolding Phase is long past — PILK is a working daemon with a full React dashboard, a registry of specialist agents, a risk-tagged tool gateway, an approval queue, a cost ledger, a memory store, an Obsidian-compatible long-term brain, and a voice pipeline.

### The dashboard

Left-nav tabs, each a real page wired to the daemon:

- **Home** — glance at what's running, pending approvals, today's spend, inbox, calendar, connected services.
- **Chat** — typed conversation with PILK. Accepts huge paste. Approvals render inline as non-modal cards. Kicks off the conversational memory interview on `?start=interview`.
- **Tasks** — status-coded gallery of sessions (a conversation = one task card). Click to expand the full chat transcript or task log.
- **Agents** — card gallery. Click an agent to see its manifest, integrations, autonomy profile, and per-agent budget. Sentinel renders as a supervisor row above the agent grid.
- **Sandboxes** — active browser contexts, process workspaces, fs scopes.
- **Approvals** — card-gallery queue grouped by risk class. Batch approve, skip, or build temporary trust rules.
- **Sentinel** — supervisor view. Live badge + chat context from the supervisor agent.
- **Cost** — spend per plan, per agent, per day / month. Estimator preview before execute.
- **Memory** — structured memory gallery (preferences / standing instructions / facts / patterns). "Analyze recent conversations" button runs Haiku distillation; "Let PILK get to know you" launches the interview.
- **Logs** — structured event log.
- **Settings** — card gallery. Coding engines, installed skills & plugins, API keys, theme, autonomy defaults, integrations panel.

### The agents

First-party agents registered on boot, each with a card on `/agents`, a manifest, scoped tools, and per-agent budgets.

| Agent | What it does |
|---|---|
| `meta_ads_agent` 📣 | Full Meta Ads operator. Builds campaigns / ad sets / ads / creatives against the Marketing API, monitors insights, pauses underperformers. Every create is `PAUSED` until explicit `FINANCIAL`-risk activation. |
| `creative_content_agent` 🎨 | On-demand image + short-form video generation. Lands assets in the workspace. |
| `sales_ops_agent` 📇 | End-to-end outbound: prospect → audit → enrich → outreach → HubSpot log → report. |
| `pitch_deck_agent` 🎯 | Builds Google Slides decks for skyway.media and clients. Delivers via the system Google account. |
| `web_design_agent` 🌐 | Static HTML + Tailwind landing pages through the design IR. Mobile-first by construction. Hands off to elementor converter for WordPress. |
| `elementor_converter_agent` 🧩 | LLM-driven design-IR → Elementor template-export JSON with validation in a tight loop. |
| `file_organization_agent` 🗂️ | Scans + tidies a sandbox workspace. Sandbox-only, never touches `$HOME`. |
| `xauusd_execution_agent` 📈 | Single-instrument XAU/USD trading agent. Top-down MTF analysis, strict filters, hard risk caps. Paper-mode only; broker + live feed gated behind `LIVE_TRADING_ENABLED`. |
| `sentinel` 🛡️ | Supervisor. Watches the other agents, surfaces anomalies, reports to PILK. |

Agents graduate to their own repo only once mature and reused (`git subtree split`). See [`agents/README.md`](agents/README.md) and the `_template` scaffold.

### Coding engines

PILK can delegate code tasks to four engines, selectable per task or per agent:

1. **Claude Code CLI** — the full Claude CLI surface, with installed skills and plugins from `~/.claude/skills/` and `~/.claude/plugins/`.
2. **Agent SDK** — Anthropic Agent SDK runtime for tighter orchestration.
3. **Codex CLI** — OpenAI Codex CLI as a fourth engine.
4. **Built-in tools** — `fs_read`, `fs_write`, `shell_exec`, `llm_ask`, etc., for lightweight jobs that don't need a full coding agent.

Settings → Coding Engines shows installed skills + plugins with descriptions and install-command hints.

### The brain + memory

Two layers, on purpose:

- **Structured memory** (`core/memory/`) — small, keyed entries (preferences, standing instructions, facts, patterns). Referenced every turn. Managed on the Memory page. Auto-learning: `POST /memory/distill` skims recent plans with Haiku and proposes durable entries for one-by-one approval.
- **Long-term brain** (`core/brain/vault.py`) — an Obsidian-compatible markdown vault at `~/PILK-brain/` (override via `PILK_BRAIN_VAULT_PATH`). PILK writes and reads `.md` notes via `brain_note_write`, `brain_note_read`, `brain_search`, `brain_note_list`. Open the same folder in Obsidian for graph / backlinks. A system-prompt nudge appends one-line daily journal entries to `daily/YYYY-MM-DD.md`.

### Risk model + approvals

Every tool declares a `RiskClass` at definition time: `READ`, `WRITE_LOCAL`, `EXEC_LOCAL`, `NET_READ`, `NET_WRITE`, `COMMS`, `FINANCIAL`, `IRREVERSIBLE`. The policy layer decides (risk × agent policy × sandbox policy × user override) whether to run, pause for approval, or hard-refuse.

Financial operations are a **hard sub-policy** — never whitelist-able, always approval-gated, typed-phrase confirmation for fund movement. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#permission--approval-model).

### Voice

Single mic pipeline, state machine (`idle → listening → transcribing → speaking`). Same orchestrator path as typed chat. Ambient listening with fine-tuning sliders surfaces in Settings.

## Layout

```
/core/          Python daemon (pilkd)
  /api/           FastAPI routes, WebSocket
  /orchestrator/  intent → plan → route
  /registry/      agent registry + loader
  /sandbox/       browser / process / fs drivers
  /tools/         tool gateway + built-in tools
  /policy/        risk classes, approval, budget, scope
  /ledger/        cost accounting + estimator
  /memory/        structured memory store
  /brain/         Obsidian vault (long-term)
  /coding/        coding engine adapters (Claude Code, Agent SDK, Codex)
  /voice/         mic pipeline, VAD, STT, TTS
  /sentinel/      supervisor agent runtime
  /integrations/  Google, Meta, LinkedIn, Slack, X, Apple, Browserbase, ...
  /governor/      budget + scope guardrails
  /secrets/       session vault interface
  main.py         pilkd entrypoint
/agents/        first-party agent templates + manifests
/ui/            Vite + React dashboard (Tauri shell later)
/portal/        sign-in at pilk.ai
/supabase/      (optional) Supabase foundation
/scripts/       dev helpers, bootstrap, db migrate
/docs/          ARCHITECTURE.md, portal.md, supabase.md
/tests/         Python tests (850+ at last count)
```

Runtime state lives outside the repo under `~/PILK/` (plans, logs, sandboxes, memory, workspace, exports) and `~/PILK-brain/` (the Obsidian vault).

## Prerequisites

- Python 3.11+
- Node 20+ and `pnpm` (or `npm`)
- macOS (Linux works for dev)
- [`uv`](https://docs.astral.sh/uv/) recommended for Python env management

## Setup

```bash
# 1. Python deps (editable install)
uv sync
# or: python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"

# 2. Create ~/PILK/ and initialize the SQLite db
python -m scripts.bootstrap_pilk_home

# 3. Dashboard deps
cd ui && pnpm install && cd ..
```

## Run

Two processes. In separate terminals:

```bash
# Terminal 1 — daemon on http://127.0.0.1:7424
uv run pilkd
# or: python -m core.main

# Terminal 2 — dashboard on http://127.0.0.1:1420
cd ui && pnpm dev
```

Open http://127.0.0.1:1420 and go to Chat.

## Configuration

Copy `.env.example` to `.env` and adjust. `~/PILK/config/` holds per-user overrides (created by the bootstrap script). API keys (Anthropic, OpenAI, Google, Meta, Browserbase, etc.) are entered in the UI at Settings → API Keys and encrypted at rest.

Supabase foundation (optional): [`docs/supabase.md`](docs/supabase.md).
Portal / sign-in at pilk.ai: [`docs/portal.md`](docs/portal.md).

## Tests

```bash
uv run pytest -q          # full suite
uv run ruff check .       # lint
uv run pytest path/to/test_file.py -q   # single file
```

## License

Proprietary. See `pyproject.toml`.
