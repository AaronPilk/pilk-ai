# sales_ops_agent

Outbound sales operator: prospecting → qualifying → enrichment →
multi-channel outreach → CRM logging → report. Runs end-to-end from a
single natural-language command.

## Example commands

- "Find CPAs in Tampa with outdated websites, send them Offer A, fill
  their contact forms with Offer A, and email me a report."
- "Prospect dental clinics in Austin. Limit to 20 with site scores over
  60. Use Offer A, email only, skip form fills."
- "Re-audit yesterday's leads — any whose site improved, mark them as
  `unsubscribed` in HubSpot."

## What it does (per run)

1. `google_places_search` for the query.
2. `site_audit` on each prospect's website.
3. `hunter_find_email` to enrich domains with emails.
4. `hubspot_search_contact` + `hubspot_upsert_contact` for every lead.
5. `gmail_send_as_me` sends the offer email.
6. `browser_session_open` + `browser_form_fill` posts the form variant.
7. `hubspot_add_note` logs the touch on each contact.
8. `gmail_send_as_me` to the operator with a markdown run report.

## Required integrations

Flip these on in **Settings → Connected accounts** (Gmail) and in
**env vars / Railway secrets** (the rest):

| Integration | Env var | Used for |
|---|---|---|
| Gmail (user role) | OAuth via Supabase | Sending outreach + report |
| Browserbase | `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID` | Form-fill |
| Google Places | `GOOGLE_PLACES_API_KEY` (or `GOOGLE_API_KEY`) | Prospecting |
| PageSpeed Insights | `PAGESPEED_API_KEY` (or `GOOGLE_API_KEY`) | Site audits |
| Hunter.io | `HUNTER_IO_API_KEY` | Email enrichment |
| HubSpot | `HUBSPOT_PRIVATE_TOKEN` | CRM upsert + notes |

A single Google Cloud API key works for Places + PageSpeed if you enable
both APIs on the project. That's the simplest setup.

## Offers

Add a YAML file to `offers/` to define a new offer. See
[`offers/offer_a.yaml`](offers/offer_a.yaml) for the template — each
offer carries qualifying criteria, an email template, and a form-fill
template. Template variables use `{{double_braces}}`.

## Safety

- CAN-SPAM: every outreach email must include a physical address +
  unsubscribe line. The offer templates enforce this; don't strip it.
- TCPA: SMS + voice are intentionally **not** wired in v1. Those arrive
  in Phase 2 along with quiet-hours + consent tracking.
- Rate limits: tools that hit external APIs surface the upstream
  rate-limit error verbatim; the agent stops and reports when it sees
  one.
- Budget: per-run cap $3, daily $15 (see `manifest.yaml`). The governor
  will kill the run before it overruns.

## Phase 2 roadmap

- Per-user BYOK: move `HUBSPOT_PRIVATE_TOKEN` + `HUNTER_IO_API_KEY` onto
  `AccountsStore` so each signed-in user supplies their own.
- SMS + voice sequences (Twilio, ElevenLabs, Deepgram).
- Learning loop: embed outcomes into `core/memory` and retrieve top-K
  similar past outreach before sending new copy.
- Multi-offer A/B: auto-split when two offer files share a `campaign`
  tag.
