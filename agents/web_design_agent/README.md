# `web_design_agent`

Designs websites and landing pages for Skyway clients. Emits static
HTML + Tailwind bundles via the design IR. Handoff to the
`elementor_converter_agent` (separate specialist) for Elementor /
WordPress deployment.

## When to use it

- "Build a landing page for Acme's new SKU."
- "Design a 4-section about page with hero, team, services, CTA."
- "Client wants a clean B2B pricing page — three tiers, feature
  parity table, FAQ."

Not the right fit for:

- "Push this page to Acme's WordPress." → `elementor_converter_agent`
  + `wordpress_push` (coming in PR D).
- "Build a pitch deck for Acme." → `pitch_deck_agent`.
- "Write marketing copy." → this agent can draft copy as part of a
  design, but isn't a copywriter.

## Example tasks

```text
Build a landing page for Acme Corporation. Hero + feature grid + CTA.
Output to /tmp/acme-landing/. Use their brand kit.
```

The agent will:

1. `fs_read clients/acme.yaml` — apply brand kit name + style notes.
2. Draft the IR structure (containers + widgets) and confirm with you.
3. Call `html_export` with the Page + `/tmp/acme-landing/`.
4. Report: file paths, section count, notable decisions.

## Tools

| Tool | Risk | Why |
|---|---|---|
| `fs_read` | READ | Load client YAML + reference docs |
| `fs_write` | READ | Save drafts / notes (real output goes via html_export) |
| `net_fetch` | NET_READ | Research reference sites |
| `browser_session_open` / `browser_navigate` / `browser_session_close` | NET_READ | Inspect live pages for reference |
| `html_export` | READ | Emit the HTML + Tailwind bundle |

## What it won't do

- Write markup by hand in its responses. The `html_export` path is
  the only exit for HTML — that's enforced by the system prompt.
- Push to WordPress. Handoff to the WordPress-capable agent.
- Convert to Elementor. Handoff to `elementor_converter_agent`.
- Guess a brand voice. If the user names a client with no YAML entry,
  the agent stops and asks.

## Budget

- Per run: $0.50
- Per day: $5.00

Matches the sales-ops agent scale. A single landing page usually runs
well under $0.20 on the standard tier.

## Next PR in the chain

- PR D wires `wordpress_push` (another tool this agent can call, once
  it knows which site to push to).
- PR B's `ClientStore` is the data source this agent's system prompt
  tells it to read; no tool-level dependency, just `fs_read` on the
  YAML file.
