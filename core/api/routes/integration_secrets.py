"""HTTP surface for the user-managed integration-secrets store.

  GET    /integration-secrets               list configured keys (no values)
  PUT    /integration-secrets/{name}        body: {value: "..."} → upsert
  DELETE /integration-secrets/{name}        clear one key

Values only travel browser → daemon. Reads return ``configured: true/false``
plus an ``updated_at`` timestamp so the dashboard can show "set 3 days ago"
next to each field without ever re-revealing the actual token.

Two ways a secret name becomes "known":

* **Static entries** in :data:`KNOWN_SECRETS` — single-tenant integrations
  where one key exists per deploy (HubSpot, Hunter, Twelve Data, etc.).
  These always appear in the list-secrets response.

* **Pattern entries** in :data:`KNOWN_SECRET_PATTERNS` — families of
  names where we can't enumerate the exact slugs at build time
  (e.g. ``wordpress_<site>_app_password`` with one slot per client site).
  Any DB row whose name matches a pattern is surfaced in the list with
  a synthesized label; callers can also PUT new names matching a
  pattern and we'll accept them.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.logging import get_logger
from core.secrets import IntegrationSecretsStore

log = get_logger("pilkd.integration_secrets")

router = APIRouter(prefix="/integration-secrets")

# Static known integrations. Each entry becomes a row in the UI and
# each tool reads by the matching name. Add a new entry when adding
# a single-tenant integration; reach for a pattern when the slug is
# per-site / per-client / per-user.
KNOWN_SECRETS: dict[str, dict[str, str | None]] = {
    "hunter_io_api_key": {
        "label": "Hunter.io",
        "description": (
            "API key from hunter.io → API section. Used to enrich "
            "domains with emails."
        ),
        "env": "HUNTER_IO_API_KEY",
    },
    "notion_api_key": {
        "label": "Notion",
        "description": (
            "Internal Integration Secret from "
            "notion.com/my-integrations. After pasting, share each "
            "page / database you want PILK to read or append to with "
            "the integration (⋯ menu on the page → Add connections). "
            "Nothing is accessible by default — Notion is opt-in "
            "per-page."
        ),
        "env": "NOTION_API_KEY",
    },
    "ghl_api_key": {
        "label": "Go High Level — Private Integration Token",
        "description": (
            "Either an agency-level PIT (Settings → Company → Private "
            "Integrations on the agency) OR a sub-account custom "
            "integration key (Settings → Integrations → Private "
            "Integrations inside the sub-account). The sub-account "
            "key is the safer default — it scopes PILK to one "
            "location instead of the whole agency. Check every scope "
            "box you want PILK to use when issuing the token. Shown "
            "once; copy and paste here."
        ),
        "env": "GHL_API_KEY",
    },
    "ghl_default_location_id": {
        "label": "Go High Level — default location id",
        "description": (
            "Default sub-account (location) id PILK operates in when "
            "a tool call doesn't specify one. Grab it from the URL of "
            "the sub-account: https://app.gohighlevel.com/location/"
            "<id>/… — paste the value between /location/ and the next "
            "slash. Length varies by tenant (typically 20 chars for "
            "current sub-accounts; older ones may be longer)."
        ),
        "env": "GHL_DEFAULT_LOCATION_ID",
    },
    "google_places_api_key": {
        "label": "Google Places",
        "description": (
            "Google Cloud API key with Places API (New) enabled. The "
            "same key works for PageSpeed if both APIs are enabled on "
            "the project."
        ),
        "env": "GOOGLE_PLACES_API_KEY",
    },
    "pagespeed_api_key": {
        "label": "PageSpeed Insights",
        "description": (
            "Google Cloud API key with PageSpeed Insights API enabled. "
            "Feel free to reuse the Places key above."
        ),
        "env": "PAGESPEED_API_KEY",
    },
    "twelvedata_api_key": {
        "label": "Twelve Data (XAU/USD feed)",
        "description": (
            "Price feed for the XAU/USD execution agent. Free tier at "
            "twelvedata.com → Dashboard → API Keys. 8 req/min limit."
        ),
        "env": "TWELVEDATA_API_KEY",
    },
    "nano_banana_api_key": {
        "label": "Nano Banana (Gemini 2.5 Flash Image)",
        "description": (
            "Google AI Studio API key for image generation. Get one at "
            "aistudio.google.com → Get API Key. The same key works as a "
            "generic Gemini key."
        ),
        "env": "NANO_BANANA_API_KEY",
    },
    "higgsfield_api_key": {
        "label": "Higgsfield (cinematic video gen)",
        "description": (
            "API key from cloud.higgsfield.ai → API Tokens. Used for "
            "text→video and image→video generations by the "
            "creative_content_agent."
        ),
        "env": "HIGGSFIELD_API_KEY",
    },
    "browserbase_api_key": {
        "label": "Browserbase API key",
        "description": (
            "API token from browserbase.com → Settings → API Keys. "
            "Used by the xauusd_execution_agent (Hugosway broker) and "
            "any other agent that needs a remote headful browser."
        ),
        "env": "BROWSERBASE_API_KEY",
    },
    "browserbase_project_id": {
        "label": "Browserbase project ID",
        "description": (
            "Project ID that scopes Browserbase sessions. Find it next "
            "to the API key on browserbase.com → Projects. Stored "
            "alongside the API key; both must be set for remote "
            "browser sessions to work."
        ),
        "env": "BROWSERBASE_PROJECT_ID",
    },
    "meta_access_token": {
        "label": "Meta Marketing API — access token",
        "description": (
            "Long-lived user access token from your Meta app "
            "(developers.facebook.com → your app → Graph API Explorer "
            "or Marketing API Setup). Scopes needed: ads_management, "
            "ads_read, business_management, pages_read_engagement. "
            "Rotate every ~60 days."
        ),
        "env": "META_ACCESS_TOKEN",
    },
    "meta_ad_account_id": {
        "label": "Meta ad account ID",
        "description": (
            "Ad account number (digits) or `act_<digits>`. Find it in "
            "Meta Ads Manager → upper-left account picker. The "
            "meta_ads_agent uses this for every campaign / ad set / ad "
            "/ insight call."
        ),
        "env": "META_AD_ACCOUNT_ID",
    },
    "meta_page_id": {
        "label": "Meta Page ID",
        "description": (
            "Owning Facebook Page that ads render as. Meta requires a "
            "page on every ad creative. Get it from facebook.com/<your-"
            "page> → About → Page Transparency, or Business Suite → "
            "Settings → Pages."
        ),
        "env": "META_PAGE_ID",
    },
    "meta_app_id": {
        "label": "Meta app ID (optional)",
        "description": (
            "App ID for the Meta app that issued the access token. Not "
            "required for day-to-day ads operations; stored so a future "
            "token-refresh helper works without a schema change."
        ),
        "env": "META_APP_ID",
    },
    "meta_app_secret": {
        "label": "Meta app secret (optional)",
        "description": (
            "App secret paired with the Meta app ID. Optional today — "
            "reserved for future token-refresh flows. Treat as sensitive."
        ),
        "env": "META_APP_SECRET",
    },
    "apify_api_token": {
        "label": "Apify API token",
        "description": (
            "Personal API token from console.apify.com → Settings → "
            "Integrations. Powers the ugc_scout_agent's IG / TikTok / "
            "Facebook creator discovery via Apify actors. Pay-per-run; "
            "starter plan ~$49/mo is plenty for weekly scouts."
        ),
        "env": "APIFY_API_TOKEN",
    },
    "arcads_api_key": {
        "label": "Arcads (UGC video generation)",
        "description": (
            "API key from app.arcads.ai → Settings → API. Powers the "
            "ugc_video_agent — script + AI actor → short-form UGC "
            "video render, ~$11 per clip at the current plan. Spec: "
            "https://external-api.arcads.ai/docs."
        ),
        "env": "ARCADS_API_KEY",
    },
    "telegram_bot_token": {
        "label": "Telegram — bot token",
        "description": (
            "Bot token from @BotFather on Telegram. Create a new bot "
            "with /newbot, copy the token it hands back. This is how "
            "PILK (and every agent) pushes notifications to the "
            "operator without waiting for the operator to initiate a "
            "conversation."
        ),
        "env": "TELEGRAM_BOT_TOKEN",
    },
    "telegram_chat_id": {
        "label": "Telegram — operator chat ID",
        "description": (
            "Numeric chat ID PILK sends to. To get yours: message the "
            "bot once, then visit "
            "https://api.telegram.org/bot<token>/getUpdates and copy "
            "the `chat.id` value from the first update."
        ),
        "env": "TELEGRAM_CHAT_ID",
    },
    "computer_control_enabled": {
        "label": "Computer control — kill switch (DANGEROUS)",
        "description": (
            "Set to 'true' to enable the IRREVERSIBLE computer_* "
            "tools: unscoped fs read + write anywhere under $HOME, "
            "unscoped shell, macOS AppleScript. Every call still "
            "needs a per-call confirmation token + normal approval + "
            "a daily-limit check, but you are authorising PILK to "
            "touch your real machine. Leave UNSET unless you "
            "explicitly want this."
        ),
        "env": "COMPUTER_CONTROL_ENABLED",
    },
    "google_ads_developer_token": {
        "label": "Google Ads — developer token",
        "description": (
            "Developer token from ads.google.com → Tools → API Center. "
            "Level test-account or basic is fine for us; we don't need "
            "standard unless we're building for external advertisers."
        ),
        "env": "GOOGLE_ADS_DEVELOPER_TOKEN",
    },
    "google_ads_client_id": {
        "label": "Google Ads — OAuth client ID",
        "description": (
            "OAuth client ID from console.cloud.google.com → APIs & "
            "Services → Credentials. A desktop-app credential works; "
            "the client secret is paired in the field below."
        ),
        "env": "GOOGLE_ADS_CLIENT_ID",
    },
    "google_ads_client_secret": {
        "label": "Google Ads — OAuth client secret",
        "description": (
            "OAuth client secret paired with the client ID above. Used "
            "with the refresh token to mint short-lived access tokens "
            "for API calls — never sent to Google's Ads API itself."
        ),
        "env": "GOOGLE_ADS_CLIENT_SECRET",
    },
    "google_ads_refresh_token": {
        "label": "Google Ads — refresh token",
        "description": (
            "Long-lived refresh token. Generate once via the OAuth "
            "Playground (adwords scope) or `gcloud auth`. Rotates "
            "rarely; the client re-mints access tokens on demand and "
            "caches them for ~50 minutes."
        ),
        "env": "GOOGLE_ADS_REFRESH_TOKEN",
    },
    "google_ads_customer_id": {
        "label": "Google Ads — customer ID",
        "description": (
            "Target Google Ads account number (digits only, no dashes). "
            "Find it in the upper-right of ads.google.com. Every "
            "campaign / ad group / ad call scopes to this customer."
        ),
        "env": "GOOGLE_ADS_CUSTOMER_ID",
    },
    "google_ads_login_customer_id": {
        "label": "Google Ads — manager ID (optional)",
        "description": (
            "MCC (manager) account ID when the refresh token was "
            "issued against a manager and the target customer is a "
            "sub-account. Leave blank if the token is on the same "
            "account as the target."
        ),
        "env": "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
    },
    # ── OAuth client credentials (for "Connect a new account") ──
    # These are the per-provider OAuth app credentials that let pilkd
    # run the Sign-in-with-X flow on the operator's behalf. Each
    # provider's dev console issues one client ID + client secret;
    # paste both halves here and the Settings → Connect panel lights
    # the chip up immediately (no env vars, no restart).
    "slack_client_id": {
        "label": "Slack — OAuth client ID",
        "description": (
            "From api.slack.com/apps → your app → Basic Information "
            "→ App Credentials → Client ID. Pairs with the client "
            "secret below to power Sign in with Slack."
        ),
        "env": "PILK_SLACK_CLIENT_ID",
    },
    "slack_client_secret": {
        "label": "Slack — OAuth client secret",
        "description": (
            "From api.slack.com/apps → your app → Basic Information "
            "→ App Credentials → Client Secret. Never leaves pilkd."
        ),
        "env": "PILK_SLACK_CLIENT_SECRET",
    },
    "linkedin_client_id": {
        "label": "LinkedIn — OAuth client ID",
        "description": (
            "From linkedin.com/developers → your app → Auth → "
            "Client ID. Pairs with the client secret below. Make "
            "sure the app has the Sign In With LinkedIn + Share on "
            "LinkedIn products enabled."
        ),
        "env": "PILK_LINKEDIN_CLIENT_ID",
    },
    "linkedin_client_secret": {
        "label": "LinkedIn — OAuth client secret",
        "description": (
            "From linkedin.com/developers → your app → Auth → "
            "Client Secret."
        ),
        "env": "PILK_LINKEDIN_CLIENT_SECRET",
    },
    "x_client_id": {
        "label": "X (Twitter) — OAuth client ID",
        "description": (
            "From developer.x.com → your project → your app → Keys "
            "and tokens → OAuth 2.0 Client ID. This is a "
            "confidential client (PKCE + client secret); the public "
            "OAuth 2.0 client ID from the same page won't work."
        ),
        "env": "PILK_X_CLIENT_ID",
    },
    "x_client_secret": {
        "label": "X (Twitter) — OAuth client secret",
        "description": (
            "From developer.x.com → your app → Keys and tokens → "
            "OAuth 2.0 Client Secret. Visible only on first issue — "
            "rotate from the same page if lost."
        ),
        "env": "PILK_X_CLIENT_SECRET",
    },
    "meta_client_id": {
        "label": "Meta (Facebook / Instagram) — OAuth app ID",
        "description": (
            "App ID from developers.facebook.com → your app → "
            "Settings → Basic → App ID. Powers Sign in with Facebook "
            "/ Instagram Business Login for posting + insights. "
            "Different from the long-lived META_ACCESS_TOKEN used by "
            "the Meta Ads agent."
        ),
        "env": "PILK_META_CLIENT_ID",
    },
    "meta_client_secret": {
        "label": "Meta (Facebook / Instagram) — OAuth app secret",
        "description": (
            "App secret from developers.facebook.com → your app → "
            "Settings → Basic → App Secret (click Show). Never leaves "
            "pilkd."
        ),
        "env": "PILK_META_CLIENT_SECRET",
    },
}


class _PatternDef:
    """One pattern entry: regex + metadata to synthesize a list row
    when a DB row matches."""

    def __init__(
        self,
        *,
        pattern: str,
        label_template: str,
        description: str,
    ) -> None:
        self.pattern = re.compile(pattern)
        self.label_template = label_template
        self.description = description

    def match(self, name: str) -> dict[str, str | None] | None:
        """Return synthesized metadata if ``name`` matches, else None.
        The label template can reference ``{slug}`` captured from the
        first group of the regex."""
        m = self.pattern.fullmatch(name)
        if m is None:
            return None
        slug = m.group(1) if m.groups() else name
        return {
            "label": self.label_template.format(slug=slug),
            "description": self.description,
            "env": None,
        }


# Pattern-based known secrets. Order is irrelevant — at most one
# pattern should match any given name.
KNOWN_SECRET_PATTERNS: list[_PatternDef] = [
    _PatternDef(
        pattern=r"wordpress_([a-z0-9][a-z0-9-]*)_app_password",
        label_template="WordPress ({slug})",
        description=(
            "Per-site WordPress credential in the form "
            "`username:app_password`. The app password comes from "
            "Users → Profile → Application Passwords on the target "
            "WordPress site. This key's ``{slug}`` should match the "
            "client's `wordpress_secret_key` in `clients/<slug>.yaml`."
        ),
    ),
]


def _match_pattern(name: str) -> dict[str, str | None] | None:
    for pat in KNOWN_SECRET_PATTERNS:
        meta = pat.match(name)
        if meta is not None:
            return meta
    return None


class SetBody(BaseModel):
    value: str = Field(
        min_length=1,
        max_length=8000,
        description="Raw API token/key. Transits over HTTPS + bearer auth.",
    )


def _store(request: Request) -> IntegrationSecretsStore:
    store = getattr(request.app.state, "integration_secrets", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="integration_secrets store offline"
        )
    return store


def _ensure_known(name: str) -> None:
    if name in KNOWN_SECRETS:
        return
    if _match_pattern(name) is not None:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            f"unknown integration secret '{name}'. "
            f"Known: {sorted(KNOWN_SECRETS)} + patterns: "
            f"{[p.pattern.pattern for p in KNOWN_SECRET_PATTERNS]}"
        ),
    )


@router.get("")
async def list_secrets(request: Request) -> dict:
    """Return every known integration and whether it's configured.

    Static entries always appear. Pattern entries appear only when a
    matching row has been written — we can't enumerate all possible
    slugs, so "configured" names show up, unconfigured pattern slots
    don't.
    """
    store = _store(request)
    have = {e.name: e.updated_at for e in store.list_entries()}
    entries: list[dict[str, Any]] = [
        {
            "name": name,
            "label": meta["label"],
            "description": meta["description"],
            "env": meta["env"],
            "configured": name in have,
            "updated_at": have.get(name),
            "testable": name in _TESTERS,
        }
        for name, meta in KNOWN_SECRETS.items()
    ]
    # Surface pattern-matched rows that exist in the DB. We can't list
    # ones that don't exist (the user has to know the slug to PUT a
    # new value), but the ones already saved should be visible.
    for db_name, updated_at in have.items():
        if db_name in KNOWN_SECRETS:
            continue
        meta = _match_pattern(db_name)
        if meta is None:
            # Row that matches neither static nor pattern — surface
            # with a neutral label so orphans are visible instead of
            # invisible.
            entries.append(
                {
                    "name": db_name,
                    "label": db_name,
                    "description": (
                        "Legacy / pattern-unmatched secret. Review + "
                        "delete if no longer used."
                    ),
                    "env": None,
                    "configured": True,
                    "updated_at": updated_at,
                }
            )
            continue
        entries.append(
            {
                "name": db_name,
                "label": meta["label"],
                "description": meta["description"],
                "env": meta["env"],
                "configured": True,
                "updated_at": updated_at,
            }
        )
    return {"entries": entries}


@router.put("/{name}")
async def set_secret(name: str, body: SetBody, request: Request) -> dict:
    _ensure_known(name)
    store = _store(request)
    try:
        store.upsert(name, body.value.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    log.info("integration_secret_upserted", name=name)
    return {"name": name, "configured": True}


@router.delete("/{name}")
async def delete_secret(name: str, request: Request) -> dict:
    _ensure_known(name)
    store = _store(request)
    removed = store.delete(name)
    log.info("integration_secret_deleted", name=name, existed=removed)
    return {"name": name, "configured": False, "removed": removed}


# Names that have a real connection-test we can run. Each entry maps
# to an async function that returns ``(ok: bool, message: str)``. Any
# name not in this map returns 400 from the test endpoint — the UI
# uses ``testable`` on the list response to decide whether to render
# the Test button at all.
async def _test_ghl(_store: IntegrationSecretsStore) -> tuple[bool, str]:
    """Cheap GHL connectivity check: list pipelines for the default
    location. 200 = token + location both work. 401 = bad token.
    404 / empty = bad location id."""
    from core.integrations.ghl.client import (
        GHLError,
        GHLNotConfiguredError,
        client_from_settings,
        resolve_location_id,
    )
    from core.integrations.ghl.tools import _default_location

    try:
        client = client_from_settings()
    except GHLNotConfiguredError as e:
        return False, str(e)

    try:
        loc = resolve_location_id(arg=None, default=_default_location())
    except GHLError:
        return False, (
            "No default location id configured. Paste the sub-account "
            "id (the value between /location/ and the next slash in "
            "your GHL URL — typically 20 chars) into the location id "
            "field above."
        )

    try:
        result = await client.pipelines_list(location_id=loc)
    except GHLError as e:
        if e.status == 401:
            return False, (
                "GHL rejected the token (401 Unauthorized). The key is "
                "either invalid, expired, or doesn't have the scopes "
                "PILK needs. Re-issue with every relevant scope checked."
            )
        if e.status == 403:
            return False, (
                "GHL refused the request (403). The token is missing "
                "a scope, or this sub-account isn't reachable from "
                "this token."
            )
        if e.status == 404:
            return False, (
                f"Location id '{loc}' not found. Double-check it's "
                "the value between /location/ and the next slash in "
                "your sub-account URL."
            )
        return False, f"GHL error {e.status}: {e.message}"
    except Exception as e:  # pragma: no cover — defensive
        return False, f"unexpected error: {type(e).__name__}: {e}"

    pipelines = result.get("pipelines") if isinstance(result, dict) else None
    count = len(pipelines) if isinstance(pipelines, list) else 0
    if count == 0:
        return True, (
            f"Connected to location '{loc}'. No pipelines defined yet "
            "in this sub-account — that's fine, the token works."
        )
    return True, (
        f"Connected to location '{loc}'. Found {count} pipeline(s) — "
        "token + location id both look correct."
    )


async def _test_hunter(_store: IntegrationSecretsStore) -> tuple[bool, str]:
    """Hunter.io: GET /v2/account?api_key=… returns plan + quota when
    the key is valid; 401 when it isn't."""
    import httpx
    from core.config import get_settings
    from core.secrets import resolve_secret

    key = resolve_secret("hunter_io_api_key", get_settings().hunter_io_api_key)
    if not key:
        return False, "hunter_io_api_key is not set."
    url = f"https://api.hunter.io/v2/account?api_key={key}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        return False, f"network error: {e}"
    if r.status_code == 401:
        return False, "Hunter rejected the key (401). Re-issue from hunter.io → API."
    if r.status_code != 200:
        return False, f"Hunter returned HTTP {r.status_code}: {r.text[:200]}"
    try:
        data = r.json().get("data", {})
        plan = data.get("plan_name", "unknown")
        searches = data.get("requests", {}).get("searches", {})
        used = searches.get("used", "?")
        avail = searches.get("available", "?")
    except Exception:
        return True, "Connected to Hunter — could not parse plan details."
    return True, f"Connected to Hunter — plan '{plan}', {used}/{avail} searches used."


async def _test_notion(_store: IntegrationSecretsStore) -> tuple[bool, str]:
    """Notion: GET /v1/users/me returns the bot's identity when the
    integration secret is valid."""
    import httpx
    from core.config import get_settings
    from core.secrets import resolve_secret

    key = resolve_secret("notion_api_key", get_settings().notion_api_key)
    if not key:
        return False, "notion_api_key is not set."
    headers = {
        "Authorization": f"Bearer {key}",
        "Notion-Version": "2022-06-28",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://api.notion.com/v1/users/me", headers=headers,
            )
    except httpx.HTTPError as e:
        return False, f"network error: {e}"
    if r.status_code == 401:
        return False, "Notion rejected the secret (401). Re-issue from notion.com/my-integrations."
    if r.status_code != 200:
        return False, f"Notion returned HTTP {r.status_code}: {r.text[:200]}"
    try:
        bot = r.json()
        name = bot.get("name") or bot.get("bot", {}).get("owner", {}).get("user", {}).get("name") or "unknown"
    except Exception:
        name = "unknown"
    return True, (
        f"Connected to Notion as '{name}'. Remember: pages/databases "
        "are opt-in — share each one with the integration before "
        "PILK can read or write to it."
    )


async def _test_apify(_store: IntegrationSecretsStore) -> tuple[bool, str]:
    """Apify: GET /v2/users/me returns account info when the token works."""
    import httpx
    from core.config import get_settings
    from core.secrets import resolve_secret

    key = resolve_secret("apify_api_token", get_settings().apify_api_token)
    if not key:
        return False, "apify_api_token is not set."
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://api.apify.com/v2/users/me", headers=headers,
            )
    except httpx.HTTPError as e:
        return False, f"network error: {e}"
    if r.status_code == 401:
        return False, "Apify rejected the token (401). Re-issue from console.apify.com → Settings → Integrations."
    if r.status_code != 200:
        return False, f"Apify returned HTTP {r.status_code}: {r.text[:200]}"
    try:
        user = r.json().get("data", {})
        username = user.get("username", "unknown")
        plan = user.get("plan", {}).get("id", "?")
    except Exception:
        return True, "Connected to Apify — could not parse account details."
    return True, f"Connected to Apify as '{username}' (plan: {plan})."


async def _test_google_places(_store: IntegrationSecretsStore) -> tuple[bool, str]:
    """Google Places (New): a single Places Text Search with a tiny
    query proves the key + the Places API enablement."""
    import httpx
    from core.config import get_settings
    from core.secrets import resolve_secret

    key = resolve_secret(
        "google_places_api_key", get_settings().google_places_api_key,
    )
    if not key:
        return False, "google_places_api_key is not set."
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": "places.displayName",
    }
    body = {"textQuery": "coffee", "pageSize": 1}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        return False, f"network error: {e}"
    if r.status_code == 403:
        return False, (
            "Google rejected the key (403) — most likely the Places "
            "API (New) isn't enabled on this Cloud project, or the "
            "key is restricted to other APIs. Enable Places API (New) "
            "in console.cloud.google.com."
        )
    if r.status_code == 400:
        return False, f"Google returned 400 (bad request): {r.text[:200]}"
    if r.status_code != 200:
        return False, f"Google Places returned HTTP {r.status_code}: {r.text[:200]}"
    return True, "Connected to Google Places (New) — Text Search returned a result."


async def _test_pagespeed(_store: IntegrationSecretsStore) -> tuple[bool, str]:
    """PageSpeed Insights: run against example.com — fastest known
    target. 200 with a lighthouse payload = key works."""
    import httpx
    from core.config import get_settings
    from core.secrets import resolve_secret

    key = resolve_secret(
        "pagespeed_api_key", get_settings().pagespeed_api_key,
    )
    if not key:
        return False, "pagespeed_api_key is not set."
    url = (
        "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
        f"?url=https://example.com&strategy=desktop&key={key}"
    )
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        return False, f"network error: {e}"
    if r.status_code == 400 and "API key not valid" in r.text:
        return False, "Google rejected the key (400 — API key not valid)."
    if r.status_code == 403:
        return False, (
            "Google rejected the key (403) — most likely PageSpeed "
            "Insights API isn't enabled on this Cloud project, or the "
            "key is restricted to other APIs."
        )
    if r.status_code != 200:
        return False, f"PageSpeed returned HTTP {r.status_code}: {r.text[:200]}"
    return True, "Connected to PageSpeed Insights — audit ran successfully against example.com."


async def _test_telegram(_store: IntegrationSecretsStore) -> tuple[bool, str]:
    """Telegram: GET /bot<token>/getMe returns the bot's identity."""
    import httpx
    from core.config import get_settings
    from core.secrets import resolve_secret

    key = resolve_secret(
        "telegram_bot_token", get_settings().telegram_bot_token,
    )
    if not key:
        return False, "telegram_bot_token is not set."
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"https://api.telegram.org/bot{key}/getMe")
    except httpx.HTTPError as e:
        return False, f"network error: {e}"
    if r.status_code == 401:
        return False, "Telegram rejected the token (401). Re-issue with @BotFather."
    if r.status_code != 200:
        return False, f"Telegram returned HTTP {r.status_code}: {r.text[:200]}"
    try:
        bot = r.json().get("result", {})
        username = bot.get("username", "unknown")
    except Exception:
        username = "unknown"
    return True, f"Connected to Telegram bot @{username}."


async def _test_browserbase(_store: IntegrationSecretsStore) -> tuple[bool, str]:
    """Browserbase: list sessions for the configured project. 200 =
    key + project both valid. 401 = bad key. 403 / 404 = project id
    doesn't match the key."""
    import httpx
    from core.config import get_settings
    from core.secrets import resolve_secret

    settings = get_settings()
    key = resolve_secret("browserbase_api_key", settings.browserbase_api_key)
    project_id = resolve_secret(
        "browserbase_project_id", settings.browserbase_project_id,
    )
    if not key:
        return False, "browserbase_api_key is not set."
    if not project_id:
        return False, "browserbase_project_id is not set."
    headers = {"X-BB-API-Key": key}
    url = f"https://api.browserbase.com/v1/sessions?projectId={project_id}&limit=1"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        return False, f"network error: {e}"
    if r.status_code == 401:
        return False, "Browserbase rejected the API key (401)."
    if r.status_code in (403, 404):
        return False, (
            "Browserbase couldn't match the project id to this key "
            f"(HTTP {r.status_code}). Double-check the project id."
        )
    if r.status_code != 200:
        return False, f"Browserbase returned HTTP {r.status_code}: {r.text[:200]}"
    return True, f"Connected to Browserbase — project '{project_id}' is reachable."


async def _test_arcads(_store: IntegrationSecretsStore) -> tuple[bool, str]:
    """Arcads: list available products / actors as a cheap auth check."""
    import httpx
    from core.secrets import resolve_secret

    # Arcads has no settings field today; resolve straight from the
    # secrets store so the dashboard-paste path works.
    key = resolve_secret("arcads_api_key", None)
    if not key:
        return False, "arcads_api_key is not set."
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                "https://external-api.arcads.ai/v1/products", headers=headers,
            )
    except httpx.HTTPError as e:
        return False, f"network error: {e}"
    if r.status_code == 401:
        return False, "Arcads rejected the key (401). Re-issue from app.arcads.ai → Settings → API."
    if r.status_code != 200:
        return False, f"Arcads returned HTTP {r.status_code}: {r.text[:200]}"
    return True, "Connected to Arcads — products endpoint is reachable."


_TESTERS: dict[str, Any] = {
    "ghl_api_key": _test_ghl,
    "ghl_default_location_id": _test_ghl,
    "hunter_io_api_key": _test_hunter,
    "notion_api_key": _test_notion,
    "apify_api_token": _test_apify,
    "google_places_api_key": _test_google_places,
    "pagespeed_api_key": _test_pagespeed,
    "telegram_bot_token": _test_telegram,
    "telegram_chat_id": _test_telegram,
    "browserbase_api_key": _test_browserbase,
    "browserbase_project_id": _test_browserbase,
    "arcads_api_key": _test_arcads,
}


@router.post("/{name}/test")
async def test_secret(name: str, request: Request) -> dict:
    """Run a live connectivity test for a configured integration.

    Returns ``{ok, message}``. ``ok=True`` means the credential
    actually works against the upstream API; ``ok=False`` means it's
    set but failed (bad scope, wrong location, expired token, etc.).
    Names without a registered tester get a 400.
    """
    _ensure_known(name)
    tester = _TESTERS.get(name)
    if tester is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"no connection test available for '{name}' yet. "
                "Implement one in core/api/routes/integration_secrets.py."
            ),
        )
    store = _store(request)
    try:
        ok, message = await tester(store)
    except Exception as e:  # pragma: no cover — defensive
        log.warning(
            "integration_secret_test_failed", name=name, error=str(e),
        )
        return {"name": name, "ok": False, "message": str(e)}
    log.info(
        "integration_secret_tested",
        name=name,
        ok=ok,
    )
    return {"name": name, "ok": ok, "message": message}
