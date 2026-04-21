# Triggers

Proactive, manifest-driven kick-offs for PILK. A trigger is a
`manifest.yaml` in a subdirectory here that binds either a **cron
schedule** or a **hub event filter** to an agent + goal. When the
trigger fires, the scheduler calls `orchestrator.agent_run(...)` —
the same entry point the Agents tab uses when you click "Run".

Turn any registered agent from reactive to autonomous by writing a
single YAML file. No Python required.

## Layout

```
triggers/
├── _template/                 # ignored (underscore prefix)
├── morning_inbox_triage/
│   └── manifest.yaml
└── README.md
```

- One folder per trigger.
- Folder name must match `manifest.yaml` `name:`.
- Underscore-prefixed folders are reserved for templates.

## Manifest schema

```yaml
name: morning_inbox_triage          # must match the folder name
description: Morning digest of what landed overnight.
agent_name: inbox_triage_agent      # must already be installed
goal: |
  Triage the inbox for anything actionable in the last 24 hours.
enabled: true                       # initial state on first boot only
schedule:
  kind: cron                        # or: kind: event
  expression: "0 7 * * *"           # 07:00 UTC every day
```

### Cron schedules

Standard 5-field cron: `minute hour day-of-month month day-of-week`.

- `*` — any
- `5` — exact value
- `1,3,5` — list
- `1-5` — range
- `*/10` — every 10th
- Day-of-week: Sunday=0 … Saturday=6 (cron convention).

Examples:

```
"0 7 * * *"      # 07:00 every day
"*/15 * * * *"   # every 15 minutes
"0 9 * * 1-5"    # 09:00 weekdays
"30 22 * * 0"    # 22:30 Sundays
```

### Event schedules

Subscribe to a hub event and fire when a filter matches.

```yaml
schedule:
  kind: event
  event_type: sentinel.incident
  filter:
    severity: HIGH
```

Filter is an **exact-match** dict on the event payload. Omit it to
fire on every event of the type.

## Operator controls

- **Enable/disable** from Settings → Triggers. Toggle persists in
  SQLite and overrides the manifest's `enabled:` on subsequent boots.
- **Run now** from Settings → Triggers — one-shot fire that ignores
  the schedule. Useful for testing.
- **Last fired** timestamp is visible in the same tab.

## Observability

Every fire broadcasts a `trigger.fired` event on the hub, so the
Activity feed shows autonomous runs alongside operator-initiated
ones. Skipped fires (orchestrator busy) emit `trigger.skipped`;
failures emit `trigger.failed`.

## Limits

- **One run at a time.** The orchestrator's lock still applies, so
  if a trigger fires while another plan is in flight, the tick is
  skipped. The next matching tick will pick up.
- **No per-trigger quotas yet.** The governor's daily cap still
  covers total spend. Per-trigger rate limiting lands if and when
  real traffic says it's needed.
- **No templating.** `goal:` is a literal string. A future revision
  may interpolate the triggering event payload (`{{ event.severity }}`)
  but the wire format stays forward-compatible.
