"""Agent manifest schema.

One `manifest.yaml` per agent folder. The orchestrator reads the manifest
to assemble a run: which tools are exposed, what sandbox to attach, what
system prompt drives the agent, what the budget caps look like. Nothing
about the manifest is negotiable at runtime — edits require a restart (or
a future hot-reload hook).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class SandboxSpec(BaseModel):
    type: Literal["process", "browser", "fs"] = "process"
    profile: str = Field(
        ...,
        description=(
            "Stable identifier for this agent's sandbox profile. Reused across "
            "runs so persistent state (cookies, a working dir) survives."
        ),
    )

    @field_validator("profile")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_PATTERN.match(v):
            raise ValueError(f"invalid profile name: {v!r}")
        return v


class Budget(BaseModel):
    per_run_usd: float = 1.00
    daily_usd: float = 5.00


class AgentPolicy(BaseModel):
    budget: Budget = Field(default_factory=Budget)


class Manifest(BaseModel):
    name: str
    version: str = "0.1.0"
    description: str = ""
    entry: str | None = None  # reserved for future programmatic agents
    system_prompt: str
    tools: list[str]
    sandbox: SandboxSpec
    policy: AgentPolicy = Field(default_factory=AgentPolicy)
    memory_namespace: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_PATTERN.match(v):
            raise ValueError(f"invalid agent name: {v!r}")
        return v

    @classmethod
    def load(cls, path: Path) -> Manifest:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"manifest {path} is not a mapping")
        return cls.model_validate(raw)
