# Sentinel

In-process supervisor that watches every other PILK agent, catches
failures, auto-fixes what's on an allowlist, and escalates everything
else. Event-driven — no polling LLM loop — so the idle cost is zero.

## How it runs

Sentinel lives inside `pilkd` itself, not as a separate process. The
FastAPI lifespan:

1. Creates a `HeartbeatStore`, `IncidentStore`, and `Notifier`.
2. Instantiates `Supervisor(...)` wired with a restart callback against
   the `AgentRegistry`.
3. Subscribes the supervisor's `on_event` to `Hub.subscribe(...)` — it
   now receives every broadcast the WebSocket clients get.
4. Starts the supervisor's 30s periodic scan task.

The four `sentinel_*` tools are registered on `ToolRegistry` at the
same time.

## Add a new rule

Rules live in `core/sentinel/rules.py`. Write an `async` function that
takes a `RuleContext` and returns `list[Finding]`, then decorate it:

```python
from core.sentinel.rules import register_rule, Finding, RuleContext

@register_rule
async def my_new_rule(ctx: RuleContext) -> list[Finding]:
    ...
    return [Finding(kind="my_new_rule", agent_name=..., summary=..., dedupe_key=...)]
```

That's it — every `Supervisor` instance that boots after import sees
the new rule. Tests can also pass an ad-hoc list via
`run_rules(ctx, rules=[my_new_rule])`.

Keep rules **cheap**. They run on every Hub event + every 30s scan. A
rule that does I/O or allocates large objects per call will tax the
hub's fan-out.

## Add a new remediation

Remediations are a fixed allowlist in
`core/sentinel/remediate.py:ALLOWED_REMEDIATIONS`. Each entry maps a
`Category` to an async function `(finding, triage, env) -> RemediationResult`.

To add one:

1. Implement the async function. Keep it idempotent — Sentinel's
   dedupe is 5 minutes, and you can still race a concurrent restart.
2. Add the `Category` → fn mapping to `ALLOWED_REMEDIATIONS`.
3. Write at least one unit test that exercises the happy path + one
   failure path.
4. Note the addition in the PR description so the pre-merge review
   pays attention to the expanded blast radius.

Anything NOT on this list stays a notification forever. That's the
whole point of the allowlist.

## Integrating an agent

Every long-running agent:

1. Calls `sentinel_heartbeat(status="ok"|"degraded"|"disabled",
   progress="...", task_id=...)` at least once per
   `heartbeat_interval_seconds` (default 60s).
2. Uses `core.logging.get_logger(...)` with `level`, `kind`, `reason`,
   `agent_name` fields — the format the `error_burst` + `crash_signature`
   rules expect.
3. (Optional) Emits a `state.json` blob via the Hub event type
   `agent.state_blob` with `{"agent": "...", "blob": {...}}` so the
   `schema_violation` rule can validate it.

See [CONTRACT.md](./CONTRACT.md) for the formal spec.

## Notifications

* Every incident writes to `sentinel_incidents` (SQLite) + appends to
  `<home>/sentinel/incidents.jsonl`.
* External webhooks fire when `SENTINEL_WEBHOOK_URL` is set AND the
  incident's severity is `high` or `critical`.
* `low` / `med` stay in the log only.

Point the webhook at whatever accepts a `POST application/json` — a
Slack incoming webhook, a PagerDuty Events API, an internal receiver.
Sentinel never retries on 4xx; one retry on 5xx with 1s delay.

## Token budget

Expected cost at idle: **0 tokens**.

Under a typical day with 2–3 distinct incidents: **< 5 000 tokens**.

Hard ceiling: `DEFAULT_DAILY_TOKEN_LIMIT = 50_000`. When the supervisor
hits 99.0% of that, it falls back to heuristic triage and logs
`sentinel_token_ceiling_reached` — the operator sees a warning and
nothing else changes. A runaway watchdog is worse than no watchdog.

## Operator cheatsheet

- "What's the state of everything?"
  → `sentinel_status`
- "What incidents have we had this hour?"
  → `sentinel_list_incidents(limit=50, only_unacked=true)`
- "I looked at that, mark it reviewed."
  → `sentinel_acknowledge_incident(incident_id="inc-...")`
- "Turn the webhook on."
  → set `SENTINEL_WEBHOOK_URL` on Railway, redeploy.

## Known limitations

- Single-tenant; per-user scoping arrives with the Phase 2 Supabase
  migration.
- Heartbeats live in SQLite, not in a fast in-memory store; the 30s
  scan is cheap enough at current agent count that this hasn't
  mattered.
- Sentinel can't restart *itself*. If the pilkd process dies, Railway
  restarts it — that's the one layer of supervision we delegate to
  the OS / platform rather than duplicate.
