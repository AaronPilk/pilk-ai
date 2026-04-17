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
    port: int = 7424
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
