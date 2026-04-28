# PILK Readiness Dashboard

Last updated: 2026-04-26 10:23:26 EDT

## 1) System health

- Backend lint: PASS (`uv run ruff check .`)
- Backend tests: PASS (`1845 passed, 1 skipped`)
- UI build: PASS (`pnpm run build` in `ui/`)
- Portal build: PASS (`npm run build` in `portal/`)

## 2) Current reality (plain English)

You are not far away. Core platform quality is strong and stable.
The main gap is not architecture; it is integration readiness and operational focus.

## 3) Security and reliability status

### Fixed in current working tree

- Cloud WebSocket auth now enforced (token required + validated).
- Cloud public-route exposure tightened (`/health` and `/version` only).
- `/version` no longer leaks local home path.
- HS256 cloud JWT now requires a strong secret (>=32 bytes).
- Video-analysis test isolation fixed (no ambient `OPENAI_API_KEY` bleed).

### Still required

- Commit and deploy these fixes to production.
- Rotate secrets after deploy (especially auth and comms keys).

## 4) Integration readiness snapshot

Source: local runtime DB (`~/PILK/pilk.db`, `integration_secrets`) + account index (`~/PILK/identity/accounts/index.json`).

### API keys configured in DB (25)

- `apify_api_token`
- `browserbase_api_key`
- `browserbase_project_id`
- `computer_control_enabled`
- `ghl_api_key`
- `ghl_default_location_id`
- `google_places_api_key`
- `higgsfield_api_key`
- `hunter_io_api_key`
- `meta_access_token`
- `meta_ad_account_id`
- `meta_app_id`
- `meta_app_secret`
- `meta_client_id`
- `meta_client_secret`
- `meta_page_id`
- `nano_banana_api_key`
- `pagespeed_api_key`
- `slack_client_id`
- `slack_client_secret`
- `telegram_bot_token`
- `telegram_chat_id`
- `twelvedata_api_key`
- `x_client_id`
- `x_client_secret`

### OAuth linked accounts

- Google `system`: CONNECTED (`gmail.send` scope present)
- Google `user`: NOT LINKED

## 5) Revenue pod readiness (most important)

### `sales_ops_agent`

- Status: PARTIAL
- Good: keys for GHL/Hunter/Places/PageSpeed exist.
- Gap: Google `user` OAuth (used for `gmail_send_as_me`) is not linked.
- Impact: outbound/reporting path is blocked or degraded.

### `lead_qualifier_agent`

- Status: READY
- Needs: GHL + PageSpeed + Places + Telegram.
- All appear configured in DB.

### `creative_content_agent`

- Status: READY
- Needs: Nano Banana + Higgsfield.
- Both appear configured in DB.

### `ugc_scout_agent`

- Status: READY
- Needs: Apify + Hunter.
- Both appear configured in DB.

### `ugc_video_agent`

- Status: BLOCKED
- Needs: `arcads_api_key`.
- Not present in integration_secrets table.

### `meta_ads_agent`

- Status: READY (integration-wise)
- Needs: Meta token + account id + page id.
- All appear configured in DB.

### `google_ads_agent`

- Status: BLOCKED
- Needs: Google Ads key set (`developer_token`, OAuth triplet, customer_id).
- Not present in integration_secrets table.

## 6) Biggest gaps to fill next

1. Link Google `user` OAuth account so `sales_ops_agent` can send as you.
2. Decide whether UGC video is in current scope; if yes, add `arcads_api_key`.
3. Decide whether Google Ads is in current scope; if yes, add full Google Ads keyset.
4. Ship/deploy the security hardening changes now (WS/auth/public routes).
5. Keep `computer_control_enabled` tightly controlled (currently enabled in DB).

## 7) 7-day focused plan (CEO-friendly)

1. Day 1: deploy security/auth patch set and rotate secrets.
2. Day 2: link Google `user` OAuth and run one full `sales_ops_agent` dry run.
3. Day 3: run live revenue workflow (prospect -> qualify -> outreach -> report).
4. Day 4: add cost guardrails by agent (daily caps + auto-pause thresholds).
5. Day 5: tune lead scoring and outreach templates from real outcomes.
6. Day 6: decide on `arcads` and Google Ads scope (on/off for this phase).
7. Day 7: produce weekly operator report (wins, misses, spend, next actions).

## 8) Success criteria for "close to autonomous"

- 3 consecutive days of fully automated sales workflow completion.
- No unauthorized COMMS or FINANCIAL actions.
- Daily spend stays under cap with no manual firefighting.
- Improvement loop is active (nightly distill + weekly policy tuning).
