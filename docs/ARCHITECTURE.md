# PILK Architecture

> This document captures the approved architecture for PILK. Every batch is
> constrained by it. When something doesn't fit, update this file first.

## What PILK is

PILK is a **local-first execution operating system for agents**. It runs as a
single desktop process on a Mac, exposes a dashboard and a voice/typed chat
interface, and orchestrates specialist agents inside scoped sandboxes. It is
not a chatbot, not an assistant shell, and not a multi-machine platform in
phase 1.

## Non-negotiables

1. **Cost-awareness in every path.** Estimate before execute, scope-gate every
   job, prefer local logic over model calls, prefer cheap models over expensive
   ones, cache deterministic results.
2. **Tiered approval at every tool call.** Every tool is tagged with a
   `RiskClass` at definition time. The policy layer decides (risk × agent
   policy × sandbox policy × user override) whether to run, pause for
   approval, or hard-refuse.
3. **Isolation by default.** Agents do not share sessions, credentials, or
   filesystem state. The tool gateway rejects out-of-sandbox paths and URLs.
4. **Single orchestrator, single plan store, single cost ledger, single
   approval queue, single agent registry.** No duplicate "brains" for voice
   vs. text. No broadcast-to-all-agents routing. No background polling loops.
5. **Built from first principles on Anthropic SDK + Playwright + FastAPI.**
   No third-party agent frameworks (OpenClaw, Manus, LangChain agents, CrewAI,
   AutoGen). Dependencies require written justification.

## Layered model

1. **Conversational layer** — voice (single mic pipeline, state machine).
2. **Precision input layer** — typed chat (first-class, accepts huge paste).
3. **Orchestrator (PILK core)** — intent normalizer → planner → executor.
4. **Agent system** — registry + manifests + lifecycle.
5. **Tool/execution layer** — terminal, fs, code edit, git, browser, APIs.
6. **Sandbox layer** — isolated browser contexts / processes / workspaces.
7. **Dashboard/control layer** — chat, tasks, agents, sandboxes, approvals,
   cost, memory, logs, settings.
8. **Memory/learning layer** — phase-gated (batch 6+).

## System map

```
Dashboard (Tauri shell · React UI · local only)
   │  loopback WebSocket + REST
   ▼
pilkd (Python / FastAPI / single process)
   Input Bus ──▶ Orchestrator ──▶ Plan Executor
                   │                   │
                   ▼                   ▼
            Agent Registry      Sandbox Manager ──▶ Sandboxes
                   │                   │
                   └────── Tool Gateway (enforces sandbox scope)
                             │
                             ▼
                        Policy Layer
                    (risk · budget · scope · approval)
                             │
                  ┌──────────┼──────────┐
                  ▼          ▼          ▼
            Cost Ledger   Approval Q   Event Log
                  │          │          │
                  └──────── SQLite ─────┘  (~/PILK/pilk.db)
```

## Directory structure

### Repository

```
/core/                         Python daemon (pilkd)
  /orchestrator/               intent → plan, planner, router
  /executor/                   step runner, retries, supervision
  /registry/                   agent registry, loader, lifecycle
  /sandbox/                    sandbox manager + drivers
  /tools/                      tool gateway + built-in tools
  /policy/                     risk classes, approval, budget, scope
  /ledger/                     cost accounting, estimator
  /memory/                     memory layer (phase-gated)
  /voice/                      single mic pipeline, TTS, VAD
  /io/                         input bus, intent normalizer
  /api/                        FastAPI routes, WebSocket
  /db/                         SQLite schema + migrations
  /config/                     settings, secrets interface
  /logging/                    structured logger
  main.py                      pilkd entrypoint
/agents/                       first-party agent templates
  /_template/                  scaffold for new agents
/ui/                           Tauri + React dashboard
/scripts/                      dev helpers, bootstrap, db migrate
/tests/
/docs/
```

Not all directories exist in batch 0. They are filled as batches land.

### Runtime (outside the repo, under `~/PILK/`)

```
~/PILK/
  pilk.db
  config/
  logs/
  memory/
  sandboxes/{sandbox_id}/
  agents/{agent_name}/
  workspace/
  exports/
  temp/
```

## Runtime model

- **One daemon (`pilkd`)** owns orchestrator, executor, registry, policy,
  ledger, WebSocket API.
- **Agents are not long-lived processes by default.** An agent is code +
  config + tools + policy. Running an agent means loading its module,
  attaching it to a sandbox, and invoking it for a scoped task. Long-running
  agents (e.g., trading_agent) get a supervised asyncio task with an explicit
  lifetime and health check.
- **Sandboxes implement one interface:** `create / attach / exec / snapshot /
  destroy / health`. Drivers in phase 1: browser (Playwright context),
  process (subprocess + scoped env), fs (convention-enforced workspace).
- **Voice is a single-task state machine** (`idle → listening → transcribing
  → speaking`). One mic pipeline. No overlapping listeners.

## Permission / approval model

Tools declare a `RiskClass` at definition time.

| Class | Examples | Default |
|---|---|---|
| `READ` | fs reads in workspace, planning | auto |
| `WRITE_LOCAL` | edit/scaffold files in workspace | auto, togglable |
| `EXEC_LOCAL` | run scripts/binaries, install deps | auto inside workspace |
| `NET_READ` | HTTP GET, read-only scraping | auto with budget gate |
| `NET_WRITE` | POST/PUT to third-party APIs | approval required |
| `COMMS` | send email, DM, post | always approval unless whitelisted + rate-capped |
| `FINANCIAL` | place trades, move funds | hard sub-policy, see below |
| `IRREVERSIBLE` | delete, force-push, drop table | always approval, always logged |

**Financial hard sub-policy.** Hard-coded, not user-configurable through
normal UI:

- `trade.execute` inside a named trading sandbox is allowed only if the
  sandbox has `trading_authority: true` AND an `account_id` allowlist.
- `funds.deposit`, `funds.withdraw`, `funds.transfer`, `payment_method.attach`,
  `bank.*` are **never auto, never whitelistable**. Each call requires a
  per-action confirmation flow with a typed phrase. No trust rule bypasses
  this.

**Approval UX primitives.** Single approve/reject, batch queue, "trust this
pattern for 1h" rules (`{agent, tool, arg_matcher, ttl}`), per-agent default
policies.

## Cost-control model

- Every LLM call and tool call writes to `cost_entries` with
  `(plan_id, step_id, agent_name, kind, model, tokens, usd)`.
- `Estimator.estimate(plan)` returns `(tokens_in, tokens_out, tool_calls,
  wall_time, usd)` per step. Dashboard shows the estimate before the user
  confirms.
- **Budget guardrails** at four levels: task, agent, day, month. Soft cap
  pauses and asks; hard cap stops.
- **Scope gates** are first-class plan inputs — e.g., `scan(region,
  max_items=N)`. `N` is a required parameter, previewed with a slider.
- **Model routing.** Opus for complex planning/long reasoning. Sonnet for
  most agent work. Haiku for routine classification. Local rules first; LLM
  only when rules are insufficient.
- **No polling.** Background work is event-driven (file watchers, webhooks,
  scheduled jobs). Never a global tick that asks the LLM what to do.
- **Caching.** Prompt caching for stable prefixes (system prompts, agent
  manifests). Result cache for deterministic tool calls.

## Agent / sandbox model

**Agent manifest** (YAML, one per agent):

```yaml
name: wholesaling_agent
version: 0.1.0
entry: src/agent.py:Agent
description: ...
tools: [browser, http, fs, llm]
sandbox:
  type: browser
  profile: wholesaling_agent
  session_refs: [county_portal_login]
policy:
  allow: [NET_READ, WRITE_LOCAL]
  approval_required: [NET_WRITE, COMMS]
  budget:
    per_run_usd: 2.00
    daily_usd: 10.00
memory:
  namespace: wholesaling
```

**Agent lifecycle:** `registered → loading → ready → running → paused →
stopped → errored`. Only the registry moves an agent between states.

**Sandbox drivers (phase 1):**

- **Browser** — Playwright `BrowserContext` with persistent profile dir at
  `~/PILK/sandboxes/{id}/profile/`.
- **Process** — subprocess in `~/PILK/sandboxes/{id}/workspace/` with env
  allowlist, cwd scope, and kill timeout.
- **Fs** — convention-enforced working directory; tools refuse out-of-scope
  paths.
- **VM/remote** (later) — same interface, different driver.

**Routing rule.** Orchestrator classifies intent → picks **one** agent (or
built-in tools if no specialist is needed). Never broadcasts. Ambiguous →
asks the user.

**Session vault.** Encrypted (libsodium secretbox, key in macOS Keychain).
Sessions are referenced by name (`session_ref`), never inlined in agent code.
Agents declare which sessions they need; policy decides whether to grant at
load time.

## UI information architecture

Single window, left rail, content pane. Muted palette, generous whitespace,
strong hierarchy. Chat is the home base.

Nav: **Chat · Tasks · Agents · Sandboxes · Approvals · Cost · Memory · Logs ·
Settings.** Top bar surfaces `{running, pending, today_spend}` so status is
one glance away. Approvals appear inline in chat (non-modal card) and
aggregate in Approvals.

## Git strategy

- Phase 1: PILK core + first-party agents live in this single repo. Agents
  are modules under `/agents/{name}/` and are installed (copy or symlink)
  into `~/PILK/agents/` on registration.
- **Promotion rule:** an agent graduates to its own repo only when it is
  mature, reused, or being externally shared. Because every agent is built as
  a self-contained folder with its own manifest from day one, promotion is
  `git subtree split` — not a rewrite.
- **Export format:** `~/PILK/exports/{agent_name}-{version}.pilkpkg` =
  agent folder + manifest + pinned requirements.

## Phased build plan

### Batch 0 — Scaffolding (this PR)
Repo layout, `pyproject.toml`, `package.json`, `.gitignore`, CI stub,
`~/PILK/` bootstrapper, SQLite schema v1, `pilkd` FastAPI skeleton (health +
WebSocket echo), structured logger, config loader, Vite + React dashboard
with left nav and a working typed-chat pane wired to the echo endpoint.
**No LLM calls, no agents, no voice.**

### Batch 1 — Orchestrator + typed chat MVP
Intent normalizer, orchestrator with a real planner (one LLM call),
executor, policy stub (`READ`/`WRITE_LOCAL` auto in workspace), cost ledger
+ estimator, built-in tools (`fs_read`, `fs_write`, `shell_exec`, `llm_ask`),
Tasks tab live, Cost tab live. End state: typed prompt → plan → execute →
report.

### Batch 2 — Agents + sandboxes
Agent registry, manifest loader, `_template` scaffolder, process and browser
sandbox drivers, tool gateway enforces sandbox scope, Agents and Sandboxes
tabs, one reference agent (`file_organization_agent`).

### Batch 3 — Approvals + risk model
Full risk-class tagging, approval queue, inline approvals in chat, batch
approvals, temporary trust rules, financial hard sub-policy, Approvals tab.

### Batch 4 — Voice pipeline
Single mic pipeline, VAD, STT, TTS, single interrupt monitor, state machine.
Same orchestrator path as typed chat. Voice state indicator + PTT in top bar.

### Batch 5 — Build mode
Structured build workflow (plan → scaffold → code → install → run → inspect
→ fix → test → register), browser-test hook, `.pilkpkg` export.

### Batch 6 — Memory + suggestions
Keyed memory (preferences, workflows), vector memory (past tasks), retrieval
in orchestrator context, Memory tab, suggestion surface in chat.

### Batch 7+ — Context awareness (opt-in), remote/VM sandboxes, multi-machine,
trading agent reference, marketplace/export polish.

## Dependency justifications (phase 1)

| Dependency | Why | Rejected alternative |
|---|---|---|
| Python + FastAPI | Mature async, rich scraping/automation ecosystem | Node — weaker scraping story |
| Anthropic SDK | LLM calls (Opus 4.7 / Sonnet 4.6 / Haiku 4.5), prompt caching | Raw HTTP — loses caching primitives |
| Playwright | Isolated browser contexts with persistent profiles | Selenium — weaker isolation |
| SQLite (`aiosqlite`) | Local single-file state, WAL mode | Postgres — unjustified overhead |
| Vite + React | Fast dev loop, small bundle | — |
| Tauri (later) | Native shell, small binary, secure IPC | Electron — heavier, weaker security |
| `pynacl` + Keychain (later) | Encrypted session vault | Plaintext — unsafe |
| `structlog` | Structured JSON logs | stdlib — less ergonomic |
