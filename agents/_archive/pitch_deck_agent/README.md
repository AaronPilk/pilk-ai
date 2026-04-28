# `pitch_deck_agent`

Builds pitch decks. Strategy-first, Slides-first. Delivers via email
from the `system` Google account when asked.

## When to use it

- "Build an investor deck for our Q4 raise."
- "Draft a 12-slide close deck for the Acme account, targeting their
  VP of Engineering."
- "Put together a working outline for the leadership offsite — I'll
  fill in the specifics."

Not the right fit for:

- "Build a landing page for the new product." → `web_design_agent`.
- "Write a long-form sales email." → free chat or
  `agent_email_deliver` directly.
- "Design the cover illustration for the deck." → a Canva path for
  this lands in a follow-up PR once the Canva MCP reconnects.

## Two output paths (one shipped, one pending)

| Path | Ships | When |
|---|---|---|
| Google Slides via `slides_create` | ✅ PR E | Default. Collaborative, editable, fast. |
| Canva via `canva_generate_presentation` | ⏳ Follow-up PR | Client-facing polished decks. Blocked on Canva MCP reconnect. |

If the user asks for Canva explicitly today, the agent says so and
offers Slides instead.

## Example task

```text
Build an investor deck for Skyway. 12 slides. Audience: seed-stage
VCs with a consumer lens. Emphasize traction and the wedge into
B2B. Email it to me when done.
```

The agent will:

1. Ask about any unknowns (ARR snapshot? wedge specifics?).
2. Draft the outline — 12 slide titles + one-line descriptions — and
   confirm.
3. Call `slides_create` with the confirmed deck.
4. Call `agent_email_deliver` with the deck URL in `links` and a
   one-line body.

## Tools

| Tool | Risk | Why |
|---|---|---|
| `fs_read` | READ | Load `clients/<slug>.yaml` + reference docs |
| `fs_write` | READ | Save outline drafts / notes |
| `net_fetch` | NET_READ | Research audience / competitors |
| `slides_create` | NET_WRITE | Generate the Google Slides deck |
| `agent_email_deliver` | NET_WRITE | Deliver the deck link |

## What the agent will never do

- Skip the outline-review step. Generation is expensive; the
  outline is the cheap place to catch misaligned briefs.
- Ship a deck without speaker notes on content slides.
- Send without the user asking.
- Guess a client's positioning. If there's no `clients/<slug>.yaml`,
  the agent stops and asks.

## Subject format for deliveries

Enforced at the `agent_email_deliver` tool layer:

```
[pitch_deck_agent] {your task description}
```

Operators can filter on `[pitch_deck_agent]` to see every delivery
this agent has sent.

## Budget

- Per run: $0.75
- Per day: $5.00

Slightly higher per-run than `web_design_agent` because decks tend to
be longer + more reasoning-heavy. Standard-tier model handles most
of the work; Opus only fires on genuinely novel pitches via the
governor's gate.

## Auto-approval

Deliveries to `aaron@skyway.media` or `pilkingtonent@gmail.com`
(individually or together) skip the approval queue — they're on the
permanent TrustStore allowlist seeded at daemon startup. Every other
recipient queues for your click in Approvals.
