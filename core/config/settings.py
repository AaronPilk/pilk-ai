"""Runtime settings for pilkd.

Reads from environment and `.env`, with sensible defaults. The PILK home
directory (default `~/PILK`) is the single source of truth for runtime
state; this module is the only place that resolves its path.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
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
