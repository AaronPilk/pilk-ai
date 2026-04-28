# `elementor_converter_agent`

Takes a design-IR Page (produced by `web_design_agent`) or an HTML
bundle and emits Elementor template-export JSON that the WordPress
Elementor plugin can import.

## Why is this an LLM-driven agent, not a pure converter?

We considered shipping a deterministic Python converter (the original
plan) but couldn't author real Elementor-JSON fixtures to test
against without a live Elementor install. Elementor's actual schema
also drifts between versions. The pragmatic choice: an LLM emits the
JSON, a narrow Pydantic validator catches the structural screw-ups,
and the agent iterates in a tight loop until the output is clean.

Tradeoff: non-deterministic output. Same IR input can produce
slightly different (but equally valid) Elementor JSON on different
runs. For a design tool that's acceptable — operators review every
push in the Approvals tab before it hits WordPress anyway.

## When to use it

- "Convert the IR file at `/tmp/acme-landing/page.json` to Elementor
  JSON and save at `/tmp/acme-landing/elementor.json`."
- "Take this HTML bundle and give me the Elementor version."

Not the right fit for:

- "Design a new landing page." → `web_design_agent`.
- "Push this Elementor JSON to Acme's WordPress." → `wordpress_push`
  tool (directly, or delegated from `web_design_agent`).

## How it operates

```
┌─────────────────────────────┐
│ 1. fs_read the IR (or HTML) │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ 2. Draft Elementor JSON     │
│    (in LLM reasoning)       │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ 3. elementor_validate       │◀──┐
│    inline, not from disk    │   │
└──────────────┬──────────────┘   │
        valid? │ no               │
               │                  │ 4. Fix specific
               │ yes              │    errors by path
               ▼                  │
┌─────────────────────────────┐   │
│ 5. fs_write to output_path  │   │
└──────────────┬──────────────┘   │
               │                  │
               └──────────────────┘
```

## Tools

| Tool | Risk | Why |
|---|---|---|
| `fs_read` | READ | Load the IR file or HTML bundle |
| `fs_write` | READ | Write the validated Elementor JSON to the target path |
| `elementor_validate` | READ | Pydantic + structural check on the draft before writing |

No `wordpress_push` in this agent — that's the next step in the
pipeline, owned by whoever wants to ship the converted JSON.

## Validation contract

The `elementor_validate` tool returns:

- `valid: bool`
- `errors: list[{loc, msg, type}]` — hard failures
- `warnings: list[{kind, path, message}]` — soft concerns (e.g.
  nested container missing `isInner=true`)
- `element_counts: {container: n, widget: n}`
- `max_depth_seen: int`

The agent iterates until `valid` is `True`. Warnings are surfaced to
the operator but don't block the write — Elementor tolerates them
and the agent explains any it chose to keep.

## Widget mapping

IR widget type → Elementor `widgetType`:

| IR | Elementor |
|---|---|
| `heading` | `heading` |
| `text` | `text-editor` |
| `button` | `button` |
| `image` | `image` |
| `spacer` | `spacer` |
| `divider` | `divider` |
| `icon` | `icon` |
| `form` | `form` |
| `video` | `video` |
| `html_embed` | `html` |

## Budget

- Per run: $0.30
- Per day: $3.00

Smaller than `web_design_agent` — the agent's reasoning is
tight (read IR → draft JSON → validate → patch → write) and cost is
dominated by a single structured-output generation.

## Follow-ups

- **Round-trip tests.** Generate IR → convert → parse back → confirm
  structural equivalence. Lands when the Elementor plugin is
  reachable for real-import testing.
- **Widget settings** — today the agent emits minimal `settings` per
  widget and lets Elementor defaults fill in the gaps. Richer
  setting mappings (typography, colors from brand kits) arrive in a
  separate PR once the Canva + ClientStore integrations mature.
