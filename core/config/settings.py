"""Runtime settings for pilkd.

Reads from environment and `.env`, with sensible defaults. The PILK home
directory (default `~/PILK`) is the single source of truth for runtime
state; this module is the only place that resolves its path.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PILK_",
        extra="ignore",
    )

    home: Path = Field(default=Path.home() / "PILK")
    host: str = "127.0.0.1"
    # Accept Railway's `PORT` (dynamic per deploy) and our own `PILK_PORT`.
    # AliasChoices resolves in order: Railway's value wins when set, else
    # the Dockerfile default (8080), else local default (7424).
    port: int = Field(
        default=7424,
        validation_alias=AliasChoices("PORT", "PILK_PORT"),
    )
    log_level: str = "INFO"

    plan_max_turns: int = 12
    shell_timeout_s: int = 30
    planner_model: str = Field(
        default="claude-haiku-4-5",
        validation_alias=AliasChoices(
            "PILK_PLANNER_MODEL", "PLANNER_MODEL", "PILK_PLANNER"
        ),
    )
    llm_ask_model: str = Field(
        default="claude-haiku-4-5",
        validation_alias=AliasChoices(
            "PILK_LLM_ASK_MODEL", "LLM_ASK_MODEL"
        ),
    )
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "PILK_ANTHROPIC_API_KEY"),
    )
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "PILK_OPENAI_API_KEY"),
    )
    # Google Gemini for the planner provider. Uses Gemini's OpenAI-
    # compatible endpoint so the OpenAI provider's code path backs it
    # too. Get a key at https://aistudio.google.com/apikey.
    # Falls through common env names (GOOGLE_API_KEY) so a single
    # Google credential covers both this and nano_banana_api_key for
    # image gen.
    gemini_planner_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PILK_GEMINI_PLANNER_API_KEY",
            "GEMINI_PLANNER_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
        ),
    )
    # xAI Grok for the planner provider. Uses xAI's OpenAI-compatible
    # endpoint at api.x.ai. Get a key at https://console.x.ai.
    grok_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PILK_GROK_API_KEY", "GROK_API_KEY", "XAI_API_KEY",
        ),
    )
    elevenlabs_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ELEVENLABS_API_KEY", "PILK_ELEVENLABS_API_KEY"),
    )
    elevenlabs_voice_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ELEVENLABS_VOICE_ID", "PILK_ELEVENLABS_VOICE_ID"
        ),
    )
    tts_voice: str | None = Field(default=None)

    # Local wake-word voice bridge — runs alongside the daemon so the
    # operator can say "Hey PILK" from anywhere on their Mac without
    # opening the web UI. Optional everywhere; we degrade silently on
    # a headless host where the hardware libs aren't importable.
    voice_bridge_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "PILK_VOICE_BRIDGE_ENABLED", "VOICE_BRIDGE_ENABLED",
        ),
    )
    picovoice_access_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PILK_PICOVOICE_ACCESS_KEY", "PICOVOICE_ACCESS_KEY",
        ),
    )
    voice_wake_keyword_path: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PILK_VOICE_WAKE_KEYWORD_PATH",
            "VOICE_WAKE_KEYWORD_PATH",
        ),
    )

    browserbase_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "BROWSERBASE_API_KEY", "PILK_BROWSERBASE_API_KEY"
        ),
    )
    browserbase_project_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "BROWSERBASE_PROJECT_ID", "PILK_BROWSERBASE_PROJECT_ID"
        ),
    )

    # ── Sales-ops agent integrations ──────────────────────────────
    # Google Places + PageSpeed share the same Google Cloud API key
    # pattern in practice; we split them so the operator can rotate
    # independently or scope them to different Cloud projects. A single
    # shared `PILK_GOOGLE_API_KEY` also satisfies both as a fallback.
    google_places_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "GOOGLE_PLACES_API_KEY",
            "PILK_GOOGLE_PLACES_API_KEY",
            "GOOGLE_API_KEY",
            "PILK_GOOGLE_API_KEY",
        ),
    )
    pagespeed_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PAGESPEED_API_KEY",
            "PILK_PAGESPEED_API_KEY",
            "GOOGLE_API_KEY",
            "PILK_GOOGLE_API_KEY",
        ),
    )
    # Hunter.io email-finder key. Used by hunter_find_email + the
    # domain-search helper for lead enrichment.
    hunter_io_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HUNTER_IO_API_KEY", "PILK_HUNTER_IO_API_KEY"
        ),
    )
    # Twelve Data — XAU/USD price-feed for the xauusd_execution_agent.
    # Free tier: 8 req/min, 800 req/day. Dashboard → API Keys ("Add
    # new") at twelvedata.com. Dashboard-paste wins over this env var.
    twelvedata_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TWELVEDATA_API_KEY", "PILK_TWELVEDATA_API_KEY"
        ),
    )

    # ── Creative-content agent integrations ───────────────────────
    # Google AI (Gemini) key for Nano Banana = `gemini-2.5-flash-image`.
    # Get one at https://aistudio.google.com/app/apikey. Falls back to
    # the generic GEMINI_API_KEY / GOOGLE_API_KEY env names.
    nano_banana_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "NANO_BANANA_API_KEY",
            "PILK_NANO_BANANA_API_KEY",
            "GEMINI_API_KEY",
            "PILK_GEMINI_API_KEY",
        ),
    )
    # Notion integration token. Create an internal integration at
    # https://www.notion.com/my-integrations → copy the "Internal
    # Integration Secret". Share each page you want PILK to access
    # with the integration (⋯ menu on the page → Add connections).
    notion_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "NOTION_API_KEY", "PILK_NOTION_API_KEY"
        ),
    )
    # Go High Level — Agency-level Private Integration Token.
    # Create at Settings → Company → Private Integrations in GHL's
    # agency view. Check EVERY scope box when issuing — the token
    # represents PILK's full access across every sub-account the
    # agency owns. Bearer auth; no OAuth flow, no token refresh.
    ghl_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "GHL_API_KEY", "PILK_GHL_API_KEY"
        ),
    )
    # Default GHL location (sub-account) id. Every GHL call is
    # scoped to a location; tools accept an optional ``location_id``
    # override per invocation but fall back here when the operator
    # hasn't specified. Grab the 24-char id from the URL of any
    # sub-account: https://app.gohighlevel.com/location/<id>/…
    ghl_default_location_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "GHL_DEFAULT_LOCATION_ID", "PILK_GHL_DEFAULT_LOCATION_ID"
        ),
    )
    # GHL API base — overridable for white-labelled reseller
    # deployments or future API-version pinning.
    ghl_api_base: str = Field(
        default="https://services.leadconnectorhq.com",
        validation_alias=AliasChoices(
            "GHL_API_BASE", "PILK_GHL_API_BASE"
        ),
    )
    # Higgsfield Cloud API key for cinematic text→video / image→video.
    # Dashboard: https://cloud.higgsfield.ai. Tokens are short-lived;
    # rotate via the dashboard-paste flow rather than baking long-lived
    # values into the deploy.
    higgsfield_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HIGGSFIELD_API_KEY", "PILK_HIGGSFIELD_API_KEY"
        ),
    )
    # Higgsfield API base — kept overridable so an enterprise tenant or
    # a regional endpoint doesn't need a code change.
    higgsfield_api_base: str = Field(
        default="https://platform.higgsfield.ai",
        validation_alias=AliasChoices(
            "HIGGSFIELD_API_BASE", "PILK_HIGGSFIELD_API_BASE"
        ),
    )

    # ── Meta Marketing API (meta_ads_agent) ───────────────────────
    # Long-lived user access token for the operator's Meta app — paste
    # it in Settings → API Keys. Meta rotates these ~every 60 days;
    # the agent surfaces 401s as a "refresh token" prompt instead of
    # crashing a plan.
    meta_access_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "META_ACCESS_TOKEN", "PILK_META_ACCESS_TOKEN",
            "FB_ACCESS_TOKEN", "FACEBOOK_ACCESS_TOKEN",
        ),
    )
    # Ad account id (digits only or with `act_` prefix — the client
    # normalises). Required for every call that touches a campaign /
    # ad set / ad / creative / insight.
    meta_ad_account_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "META_AD_ACCOUNT_ID", "PILK_META_AD_ACCOUNT_ID",
            "FB_AD_ACCOUNT_ID",
        ),
    )
    # Owning Facebook Page id — Meta requires one on every ad creative
    # because ads render as posts "by" a page. Can be overridden per
    # creative via the tool's `page_id` arg.
    meta_page_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "META_PAGE_ID", "PILK_META_PAGE_ID", "FB_PAGE_ID",
        ),
    )
    # App id + secret for the Meta app backing the long-lived token.
    # Not used by the current client (we rely on the pre-minted user
    # token), but stored so a future token-refresh helper or OAuth
    # flow doesn't need schema changes.
    meta_app_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "META_APP_ID", "PILK_META_APP_ID", "FB_APP_ID",
        ),
    )
    meta_app_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "META_APP_SECRET", "PILK_META_APP_SECRET", "FB_APP_SECRET",
        ),
    )

    # ── Telegram (system-wide push channel from PILK → operator) ─
    # Any agent — or PILK itself — can ping the operator via Telegram
    # when it needs a human-in-the-loop (approval request, campaign
    # report ready, sentinel incident, etc.). Single-tenant: one bot
    # + one chat_id = one operator. Per-user bots land later alongside
    # the rest of the BYOK story.
    telegram_bot_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TELEGRAM_BOT_TOKEN", "PILK_TELEGRAM_BOT_TOKEN",
        ),
    )
    telegram_chat_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TELEGRAM_CHAT_ID", "PILK_TELEGRAM_CHAT_ID",
        ),
    )
    # Quiet-hours window during which proactive (unsolicited) pings
    # are suppressed. Replies to operator-initiated messages always
    # go through — this only gates things like proactive_checkin,
    # approval-waiting nudges, and sentinel notifications. Format is
    # ``HH:MM-HH:MM`` in 24-hour local time; set to ``"off"`` to
    # disable the gate entirely. Ranges that wrap midnight are
    # supported (the default 22:00-08:00 is exactly that).
    quiet_hours_local: str = Field(
        default="22:00-08:00",
        validation_alias=AliasChoices(
            "PILK_QUIET_HOURS", "PILK_QUIET_HOURS_LOCAL",
        ),
    )
    # Timezone name (IANA form, e.g. ``America/Chicago``) the quiet-
    # hours window is evaluated in. Empty / invalid values fall back
    # to UTC — callers never see this, we just log a warning and keep
    # running.
    quiet_hours_tz: str = Field(
        default="America/Chicago",
        validation_alias=AliasChoices(
            "PILK_QUIET_HOURS_TZ", "PILK_TZ",
        ),
    )
    # Bidirectional chat bridge: when on (default), pilkd long-polls
    # Telegram getUpdates for inbound messages from the configured
    # chat_id and feeds each one into the orchestrator's free-chat
    # path, mirroring the assistant reply back over Telegram. Off =
    # Telegram stays push-only (the tool family still works for
    # notifications, but inbound messages are ignored). Auto-disables
    # itself when either telegram_bot_token or telegram_chat_id is
    # missing so a partially-configured bot doesn't spam error logs.
    telegram_chat_bridge_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "PILK_TELEGRAM_CHAT_BRIDGE_ENABLED",
            "TELEGRAM_CHAT_BRIDGE_ENABLED",
        ),
    )

    # ── Apify (ugc_scout_agent — IG / TikTok / Facebook scraper) ─
    # Apify personal API token from console.apify.com → Settings →
    # Integrations. The UGC scout agent drives actors like
    # apify/instagram-scraper and clockworks/tiktok-scraper through
    # this key. Single-tenant; per-user keys can land later alongside
    # the rest of the BYOK story.
    apify_api_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "APIFY_API_TOKEN", "PILK_APIFY_API_TOKEN",
        ),
    )

    # ── Computer control (IRREVERSIBLE) ──────────────────────────
    # Kill switch for the computer_* tool family: unscoped fs_read /
    # fs_write / shell / osascript. Must be explicitly set to "true"
    # for the tools to run; any other value (including unset) leaves
    # them inert. Paired with a daily-call limit so even when
    # enabled, PILK can't run away.
    computer_control_enabled: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "COMPUTER_CONTROL_ENABLED",
            "PILK_COMPUTER_CONTROL_ENABLED",
        ),
    )
    computer_control_daily_limit: int = Field(
        default=20,
        validation_alias=AliasChoices(
            "COMPUTER_CONTROL_DAILY_LIMIT",
            "PILK_COMPUTER_CONTROL_DAILY_LIMIT",
        ),
    )

    # ── Google Ads (google_ads_agent) ─────────────────────────────
    # Five secrets power the full operator. The developer token
    # authenticates your Google Ads MCC against the API; the OAuth
    # triplet (client_id / client_secret / refresh_token) mints short-
    # lived access tokens the client uses per call; customer_id names
    # the ad account being operated on. login_customer_id is only
    # needed when the token was issued against a manager account and
    # the target is a sub-account underneath it.
    google_ads_developer_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "GOOGLE_ADS_DEVELOPER_TOKEN", "PILK_GOOGLE_ADS_DEVELOPER_TOKEN",
        ),
    )
    google_ads_client_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "GOOGLE_ADS_CLIENT_ID", "PILK_GOOGLE_ADS_CLIENT_ID",
        ),
    )
    google_ads_client_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "GOOGLE_ADS_CLIENT_SECRET", "PILK_GOOGLE_ADS_CLIENT_SECRET",
        ),
    )
    google_ads_refresh_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "GOOGLE_ADS_REFRESH_TOKEN", "PILK_GOOGLE_ADS_REFRESH_TOKEN",
        ),
    )
    google_ads_customer_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "GOOGLE_ADS_CUSTOMER_ID", "PILK_GOOGLE_ADS_CUSTOMER_ID",
        ),
    )
    google_ads_login_customer_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
            "PILK_GOOGLE_ADS_LOGIN_CUSTOMER_ID",
        ),
    )

    # ── Coding engines ────────────────────────────────────────────
    # Path (or bare command name on PATH) for the local Claude Code
    # binary. When resolvable and responsive, the ClaudeCodeBridge
    # engine delegates coding tasks to it so they're billed against
    # the operator's Claude subscription instead of PILK's per-token
    # API budget. Legacy alias `PILK_CLAUDE_CODE_BRIDGE_URL` is kept
    # so existing deploys don't need to rename their env var.
    claude_code_binary: str = Field(
        default="claude",
        validation_alias=AliasChoices(
            "PILK_CLAUDE_CODE_BINARY",
            "CLAUDE_CODE_BINARY",
            "PILK_CLAUDE_CODE_BRIDGE_URL",
            "CLAUDE_CODE_BRIDGE_URL",
        ),
    )
    # Upper bound on agentic turns per Claude Code run. The CLI has
    # no default cap, so leaving this unset risks an autonomous loop
    # burning the operator's whole work-session. 10 is plenty for
    # repo-scope refactors and short enough to notice when something's
    # stuck.
    claude_code_max_turns: int = Field(
        default=10,
        validation_alias=AliasChoices(
            "PILK_CLAUDE_CODE_MAX_TURNS", "CLAUDE_CODE_MAX_TURNS"
        ),
    )
    # Passed straight to `claude --permission-mode`. Default is
    # `bypassPermissions` because PILK already held a single approval
    # for the delegated task; per-tool prompts would stall in headless
    # mode. Set `acceptEdits` for a more conservative posture that
    # still auto-approves file edits, or `plan` to force Claude Code
    # to emit a plan without executing.
    claude_code_permission_mode: str = Field(
        default="bypassPermissions",
        validation_alias=AliasChoices(
            "PILK_CLAUDE_CODE_PERMISSION_MODE",
            "CLAUDE_CODE_PERMISSION_MODE",
        ),
    )
    # Optional hard per-run spend cap forwarded as `--max-budget-usd`.
    # Subscription runs report $0 cost; this only matters when the CLI
    # is authed with an API key (e.g. a workstation without a Claude
    # subscription logged in). Leave at 0 / unset to disable.
    claude_code_max_budget_usd: float = Field(
        default=0.0,
        validation_alias=AliasChoices(
            "PILK_CLAUDE_CODE_MAX_BUDGET_USD",
            "CLAUDE_CODE_MAX_BUDGET_USD",
        ),
    )
    # Optional model override forwarded as `--model`. Accepts a short
    # alias (`sonnet`, `opus`, `haiku`) or a full model name. Unset =
    # whatever Claude Code's own default is.
    claude_code_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PILK_CLAUDE_CODE_MODEL", "CLAUDE_CODE_MODEL"
        ),
    )
    # ── Codex (OpenAI) bridge ────────────────────────────────────
    # Companion to the Claude Code bridge. When the `codex` CLI is
    # installed and the operator has run `codex login`, runs bill
    # against their ChatGPT subscription instead of PILK's API budget.
    # Same accept-a-path-or-name convention as claude_code_binary.
    codex_binary: str = Field(
        default="codex",
        validation_alias=AliasChoices(
            "PILK_CODEX_BINARY", "CODEX_BINARY"
        ),
    )
    # Default permission posture: `--full-auto` gives Codex
    # workspace-write with on-request approvals — the closest analogue
    # to Claude Code's `bypassPermissions` without going all the way
    # to `--yolo`. Flip `codex_yolo` to true for trusted local runs
    # where PILK has already approved at the task level.
    codex_yolo: bool = Field(
        default=False,
        validation_alias=AliasChoices("PILK_CODEX_YOLO", "CODEX_YOLO"),
    )
    # Explicit sandbox mode override. When set, takes precedence over
    # `codex_yolo` and the full-auto default. Accepts: read-only,
    # workspace-write, danger-full-access.
    codex_sandbox_mode: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PILK_CODEX_SANDBOX_MODE", "CODEX_SANDBOX_MODE"
        ),
    )
    # Optional model override forwarded as `--model`. Unset = whatever
    # Codex's own default is (usually the latest `gpt-5-codex` family).
    codex_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PILK_CODEX_MODEL", "CODEX_MODEL"
        ),
    )

    # ── Brain vault (Obsidian-compatible) ─────────────────────────
    # Directory of markdown notes that serves as PILK's long-term
    # knowledge store. The operator can open the same directory as an
    # Obsidian vault for graph + backlink navigation. Auto-created on
    # boot if missing; seeded with a starter README.
    brain_vault_path: Path = Field(
        default_factory=lambda: Path.home() / "PILK-brain",
        validation_alias=AliasChoices(
            "PILK_BRAIN_VAULT_PATH", "BRAIN_VAULT_PATH"
        ),
    )
    # Auto-ingest ~/.claude/projects/ into the brain vault on boot.
    # Idempotent — writes at stable vault paths so re-runs overwrite
    # rather than duplicate. Off means the operator has to ask PILK
    # in chat ("ingest my Claude Code transcripts") to seed the
    # vault. Default on so the vault is useful immediately after
    # first boot.
    brain_auto_ingest_on_boot: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "PILK_BRAIN_AUTO_INGEST_ON_BOOT",
            "BRAIN_AUTO_INGEST_ON_BOOT",
        ),
    )
    # Gmail auto-ingest is gated separately because it hits the
    # network AND can produce hundreds of notes on a large inbox.
    # Default OFF so a fresh install doesn't blast the vault with
    # 90 days of email before the operator sees the Brain page for
    # the first time. Flip to true (in .env or via an env var) once
    # you've linked Google and want the inbox in the brain.
    brain_auto_ingest_gmail_on_boot: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "PILK_BRAIN_AUTO_INGEST_GMAIL_ON_BOOT",
            "BRAIN_AUTO_INGEST_GMAIL_ON_BOOT",
        ),
    )
    brain_auto_ingest_gmail_query: str = Field(
        default="newer_than:30d",
        validation_alias=AliasChoices(
            "PILK_BRAIN_AUTO_INGEST_GMAIL_QUERY",
            "BRAIN_AUTO_INGEST_GMAIL_QUERY",
        ),
    )
    # Keep the ChatGPT per-conversation side-index warm. Builds once
    # on boot + nightly at 03:00 local. Default ON; it's a cheap
    # keyword pass, no network, no LLM calls.
    chatgpt_index_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "PILK_CHATGPT_INDEX_ENABLED",
            "CHATGPT_INDEX_ENABLED",
        ),
    )
    # Dedicated model for the draft-only APIEngine. Defaults to the
    # standard tier so the governor can re-route via its normal rules
    # later without a settings change.
    coding_api_model: str = Field(
        default="claude-haiku-4-5",
        validation_alias=AliasChoices(
            "PILK_CODING_API_MODEL", "CODING_API_MODEL"
        ),
    )

    # ── Governor: tiered model routing + cost caps ────────────────
    # Tier slot shape: (provider, model). Each tier targets a backend
    # picked for the right load profile:
    #
    #   LIGHT    → OpenAI gpt-4o-mini. Cheap (~$0.15/M input), fast,
    #              and crucially on a SEPARATE rate-limit bucket from
    #              Anthropic — so high-volume conversational chatter
    #              never eats the Max-subscription budget that
    #              STANDARD relies on. Falls back to whichever
    #              provider the orchestrator can resolve when the
    #              OpenAI key is missing (anthropic API → claude_code
    #              CLI → nothing).
    #   STANDARD → Claude Code CLI (Max subscription, $0 marginal).
    #              Bulk balanced reasoning rides the plan the
    #              operator already pays $200/mo for. Image-bearing
    #              turns auto-bypass to the Anthropic API (the CLI
    #              has no vision surface).
    #   PREMIUM  → Anthropic API (Opus). Rare deep-reasoning work;
    #              kept on the API so adaptive thinking + vision
    #              stay first-class.
    #
    # Override any slot via the matching env vars below.
    tier_light_provider: str = Field(
        default="openai",
        validation_alias=AliasChoices("PILK_TIER_LIGHT_PROVIDER", "TIER_LIGHT_PROVIDER"),
    )
    tier_light_model: str = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices("PILK_TIER_LIGHT_MODEL", "TIER_LIGHT_MODEL"),
    )
    # Master switch for the whole subscription-backed chat path. Off
    # means build_providers doesn't register the claude_code provider
    # at all, even if the binary is available — every turn lands on
    # the API.
    enable_claude_code_chat: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "PILK_ENABLE_CLAUDE_CODE_CHAT", "ENABLE_CLAUDE_CODE_CHAT",
        ),
    )
    tier_standard_provider: str = Field(
        # Default to the Claude Code CLI — same subscription path LIGHT
        # already uses — so balanced-tier work (the bulk of real
        # orchestrator traffic) runs against the operator's Max plan
        # instead of burning API credits. Anthropic still registers as
        # a fallback for image turns (the orchestrator bypasses
        # claude_code when vision is needed) and for setups without
        # the claude binary on PATH. Set back to "anthropic" to opt
        # out of subscription-first balanced chat.
        default="claude_code",
        validation_alias=AliasChoices(
            "PILK_TIER_STANDARD_PROVIDER", "TIER_STANDARD_PROVIDER"
        ),
    )
    tier_standard_model: str = Field(
        default="claude-sonnet-4-6",
        validation_alias=AliasChoices(
            "PILK_TIER_STANDARD_MODEL", "TIER_STANDARD_MODEL"
        ),
    )
    tier_premium_provider: str = Field(
        default="anthropic",
        validation_alias=AliasChoices(
            "PILK_TIER_PREMIUM_PROVIDER", "TIER_PREMIUM_PROVIDER"
        ),
    )
    tier_premium_model: str = Field(
        default="claude-opus-4-7",
        validation_alias=AliasChoices(
            "PILK_TIER_PREMIUM_MODEL", "TIER_PREMIUM_MODEL"
        ),
    )
    daily_cap_usd: float = Field(
        default=5.00,
        validation_alias=AliasChoices("PILK_DAILY_CAP_USD", "DAILY_CAP_USD"),
    )
    premium_gate: str = Field(
        default="ask",
        validation_alias=AliasChoices("PILK_PREMIUM_GATE", "PREMIUM_GATE"),
    )
    # Drives the Claude Max subscription usage bar in the dashboard
    # header. Anthropic does not publish the real cap; 225 is the
    # ballpark Max-plan 5-hour soft limit reported by the community.
    # When you start hitting the cap, adjust this down to match what
    # Anthropic actually enforces on your account.
    max_messages_per_5h: int = Field(
        default=225,
        validation_alias=AliasChoices(
            "PILK_MAX_MESSAGES_PER_5H", "MAX_MESSAGES_PER_5H",
        ),
    )

    # ── Google / Gmail integration ───────────────────────────────
    # Path to the OAuth client secret JSON downloaded from Google
    # Cloud (Credentials → OAuth client ID → Desktop → download).
    google_client_secret_path: Path = Field(
        default=Path("pilk-google-client.json"),
        validation_alias=AliasChoices(
            "PILK_GOOGLE_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET"
        ),
    )

    # ── Supabase (foundation — not used at runtime yet) ────────────
    # All four fields are optional. When unset, PILK runs exactly as
    # it does today (SQLite only, no remote auth). When set, later
    # batches will use them for auth, workspace membership, and
    # storage — without moving the local runtime off SQLite.
    supabase_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SUPABASE_URL", "PILK_SUPABASE_URL"),
    )
    supabase_anon_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SUPABASE_ANON_KEY", "PILK_SUPABASE_ANON_KEY"
        ),
    )
    # Server-only; never sent to the browser. Used for migrations and
    # privileged backfills. Rotatable without touching the anon key.
    supabase_service_role_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SUPABASE_SERVICE_ROLE_KEY", "PILK_SUPABASE_SERVICE_ROLE_KEY"
        ),
    )
    # Master admin bootstrap: seed migration reads this to insert the
    # owner row. No enforcement logic here — the seed only runs if
    # the table is empty, so rotating the env var later doesn't clone
    # ownership.
    supabase_master_admin_email: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SUPABASE_MASTER_ADMIN_EMAIL", "PILK_SUPABASE_MASTER_ADMIN_EMAIL"
        ),
    )
    # Supabase JWT secret — used server-side to verify Bearer tokens from
    # the portal. Required when PILK_CLOUD=1. Never expose to the browser.
    # Found in Supabase dashboard → Project Settings → API → JWT Secret.
    supabase_jwt_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SUPABASE_JWT_SECRET", "PILK_SUPABASE_JWT_SECRET"
        ),
    )

    # ── Cloud mode ────────────────────────────────────────────────
    # When 1 (the Fly.io deploy sets this), pilkd runs as a public API:
    # Bearer-token auth on every request, CORS locked to cloud_origins,
    # and local-only integrations (Apple Messages, local sandboxes) are
    # skipped at startup. Defaults to 0 so `pilkd` still runs identically
    # on a laptop for development.
    cloud: bool = Field(
        default=False,
        validation_alias=AliasChoices("PILK_CLOUD", "CLOUD"),
    )
    # Comma-separated origins allowed to hit the API in cloud mode.
    # `pilk.ai` is the marketing + auth portal; `app.pilk.ai` is where the
    # dashboard (`ui/`) lives after Phase 1b. Local-mode CORS ignores this
    # and uses a fixed list of 127.0.0.1 ports instead.
    cloud_origins: str = Field(
        default="https://pilk.ai,https://app.pilk.ai",
        validation_alias=AliasChoices("PILK_CLOUD_ORIGINS", "CLOUD_ORIGINS"),
    )

    @property
    def allowed_origins(self) -> list[str]:
        if self.cloud:
            return [o.strip() for o in self.cloud_origins.split(",") if o.strip()]
        return [
            "http://127.0.0.1:1420",
            "http://localhost:1420",
            "http://127.0.0.1:1421",
            "http://localhost:1421",
        ]

    @property
    def db_path(self) -> Path:
        return self.home / "pilk.db"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def config_dir(self) -> Path:
        return self.home / "config"

    @property
    def sandboxes_dir(self) -> Path:
        return self.home / "sandboxes"

    @property
    def agents_dir(self) -> Path:
        return self.home / "agents"

    @property
    def workspace_dir(self) -> Path:
        return self.home / "workspace"

    @property
    def memory_dir(self) -> Path:
        return self.home / "memory"

    @property
    def integrations_dir(self) -> Path:
        return self.home / "identity" / "integrations"

    @property
    def google_credentials_path(self) -> Path:
        """Legacy single-account path (pre-Batch-K).

        Retained only so the migration step on startup knows where to
        look. New code should call `google_role_path(role)` instead.
        """
        return self.integrations_dir / "google.json"

    def google_role_path(self, role: str) -> Path:
        """Per-role OAuth refresh-token path.

        role is "system" (PILK's operational mail) or "user" (your
        real working mail). Files live under
        ~/PILK/identity/integrations/google/{role}.json.
        """
        if role not in ("system", "user"):
            raise ValueError(f"unknown google role: {role}")
        return self.integrations_dir / "google" / f"{role}.json"

    @property
    def exports_dir(self) -> Path:
        return self.home / "exports"

    @property
    def temp_dir(self) -> Path:
        return self.home / "temp"

    def resolve_home(self) -> Path:
        return self.home.expanduser().resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
