# Agent Monitoring Contract

Every long-running agent that wants to be watched by **Sentinel** must
satisfy the three contract clauses below. They are deliberately minimal —
a single tool call, a known log format, and a state-file shape.

Anything NOT implementing this contract is still visible to Sentinel
(via Hub events for plans it runs), but Sentinel can't detect stuck
loops or crashed heartbeats for it.

## Clause 1 — Heartbeat

Every agent loop calls the `sentinel_heartbeat` tool at least once per
`heartbeat_interval_seconds` (declared in `manifest.yaml`, default 60s).

```
sentinel_heartbeat(
  agent_name="xauusd_execution_agent",  # optional — defaults to ctx.agent_name
  status="ok" | "degraded" | "disabled",
  progress="one-line current task",     # optional, 160-char cap
  task_id="...",                        # optional, ties to stuck-task detection
)
```

Sentinel flags any agent whose latest heartbeat is older than
`2 × heartbeat_interval_seconds` as `stale_heartbeat`. Exception: agents
in state `DISABLED` or `OFF` are exempt — they're supposed to be quiet.

### Why a DB table instead of a file?

`agents/<name>/heartbeat` touch-files would work on a single host but
break on Railway's stateless containers and across the planned
multi-tenant migration. The `agent_heartbeats` SQLite table is cheap
(≤N rows ever) and survives restarts.

## Clause 2 — Log format

Agents write structured logs via `core.logging.get_logger(...)`. Sentinel
parses:

| key | meaning |
|---|---|
| `level` | `info | warning | error | critical` |
| `agent_name` | which agent emitted the event |
| `kind` | category of event (`state_transition`, `tool_error`, `safety_interrupt`, …) |
| `reason` | always present on any error-level log |

Sentinel's `error_burst` rule counts `level in {error, critical}` lines
per agent over a 60-second sliding window. Default threshold is 5.

**Anti-pattern:** loud warnings in a tight loop. Sentinel won't escalate
on warnings alone, but repeated warnings + any error will.

## Clause 3 — `state.json` schema (optional but encouraged)

Agents that track a state machine (`xauusd_execution_agent` is the
reference implementation) write a `state.json` into their data
directory with this shape:

```json
{
  "agent_name": "xauusd_execution_agent",
  "version": 1,
  "state": "SCANNING",
  "execution_mode": "approve",
  "updated_at": "2026-04-19T03:16:20Z",
  "active_task_id": null,
  "metadata": {}
}
```

If present, Sentinel validates it against the declared schema and
flags `schema_violation` on mismatch.

## Declaring the contract in `manifest.yaml`

```yaml
monitoring:
  heartbeat_interval_seconds: 60   # lower = Sentinel wakes more often
  stuck_task_timeout_seconds: 900  # 15m default
  stop_severity: "critical"        # auto-DISABLE on what severity
```

Absence of `monitoring:` = opt-out. Sentinel won't flag the agent for
heartbeat or stuck-task failures; Hub-event rules still apply.

## What Sentinel will NEVER do automatically

- Delete agent files or databases.
- Call any `FINANCIAL`-class tool (no flattening trades on your behalf
  via remediation — operator-only).
- Edit an agent's manifest.
- Escalate privileges on a non-DISABLED agent.

Every remediation not on Sentinel's allowlist becomes a notification
event and stops there.
