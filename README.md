# PILK

**Local-first execution OS for agents.** PILK is a personal orchestrator that plans, routes, and supervises work across specialist agents running inside scoped sandboxes — running entirely on a single Mac, with cost-awareness and tiered approval baked into every tool call.

This repository is a greenfield build. See `docs/ARCHITECTURE.md` for the full system design.

## Status

**Batch 0: scaffolding.** The daemon boots, the dashboard boots, the typed-chat pane echoes messages over a WebSocket. No LLM calls, no agents, no voice yet. The skeleton is here to verify shape before behavior goes in.

## Layout

```
/core/       Python daemon (pilkd): orchestrator, executor, registry, sandboxes, tools, policy, ledger, api
/agents/     First-party agent templates (phase 2+)
/ui/         Vite + React dashboard (Tauri shell added in a later batch)
/scripts/    Dev helpers, bootstrap, db migrate
/docs/       Architecture and authoring docs
/tests/      Python tests
```

At runtime, PILK's state and working directory live outside this repo under `~/PILK/` (plans, logs, sandboxes, agents, memory, workspace, exports).

## Prerequisites

- Python 3.11+
- Node 20+ and `pnpm` (or `npm`)
- macOS (Linux works for dev)

## Setup

```bash
# 1. Install Python deps (editable)
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Create ~/PILK/ and initialize the SQLite db
python -m scripts.bootstrap_pilk_home

# 3. Install dashboard deps
cd ui && pnpm install && cd ..
```

## Run

Two processes. In separate terminals:

```bash
# Terminal 1: core daemon on http://127.0.0.1:7424
python -m core.main

# Terminal 2: dashboard on http://127.0.0.1:1420
cd ui && pnpm dev
```

Open http://127.0.0.1:1420, go to Chat, type a message — the daemon echoes it back through the WebSocket.

## Configuration

Copy `.env.example` to `.env` and adjust. `~/PILK/config/` holds per-user overrides (created by the bootstrap script).

## What's next

Batch 1 wires the orchestrator, planner, executor, cost ledger, and built-in filesystem/shell/LLM tools into the chat pane so typed prompts actually perform work. See `docs/ARCHITECTURE.md` for the phased build plan.
