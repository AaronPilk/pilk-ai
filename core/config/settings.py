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
    # HubSpot Private App access token — simpler than OAuth for v1.
    # Create in HubSpot → Settings → Integrations → Private Apps and
    # grant contact + note scopes. Single-tenant for now; Phase 2 will
    # move this onto AccountsStore so each user brings their own.
    hubspot_private_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "HUBSPOT_PRIVATE_TOKEN", "PILK_HUBSPOT_PRIVATE_TOKEN"
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

    # ── Coding engines ────────────────────────────────────────────
    # Claude Code runs locally via a bridge when the user has one set
    # up; unset = not available, PILK falls back to the API engine.
    claude_code_bridge_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PILK_CLAUDE_CODE_BRIDGE_URL", "CLAUDE_CODE_BRIDGE_URL"
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
    # Tier slot shape: (provider, model). Batch C executes only the
    # Anthropic provider; a non-anthropic provider is architecturally
    # accepted and logged as a fallback until the OpenAI execution path
    # lands in Batch D. Defaults intentionally map every tier to a
    # concrete Claude model so a fresh install routes sanely.
    tier_light_provider: str = Field(
        default="anthropic",
        validation_alias=AliasChoices("PILK_TIER_LIGHT_PROVIDER", "TIER_LIGHT_PROVIDER"),
    )
    tier_light_model: str = Field(
        default="claude-haiku-4-5",
        validation_alias=AliasChoices("PILK_TIER_LIGHT_MODEL", "TIER_LIGHT_MODEL"),
    )
    tier_standard_provider: str = Field(
        default="anthropic",
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
