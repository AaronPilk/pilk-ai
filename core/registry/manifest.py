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
    capabilities: list[str] = Field(
        default_factory=list,
        description=(
            "Opt-in capability flags this sandbox carries. Policy checks "
            "them for class-specific overrides (e.g. 'trading' unlocks "
            "trade_execute). Unknown flags are ignored."
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


class IntegrationSpec(BaseModel):
    """A third-party dependency this agent needs before it can run.

    Two flavors:

    * ``api_key`` — a string the operator pastes into Settings → API Keys
      (lives in ``integration_secrets``). The manifest's ``name`` is the
      row key; env fallbacks still apply.
    * ``oauth`` — an OAuth-provider account the operator links via
      ``/integrations/oauth/<provider>``. The manifest's ``name`` is the
      provider id (google, slack, linkedin, ...); ``role`` picks
      which account binding within the provider (e.g. "me" vs
      "system"-sender).

    The UI renders each integration inline on the agent detail panel
    with a "configured ✓" chip or an inline input/Connect button, so the
    operator never has to hunt for where to paste a key.
    """

    name: str = Field(
        ...,
        description=(
            "For api_key: the integration_secrets row key (e.g. "
            "'higgsfield_api_key'). For oauth: the provider id "
            "(e.g. 'google')."
        ),
    )
    kind: Literal["api_key", "oauth"]
    label: str = Field(
        ...,
        description="Human-readable name rendered in the UI.",
    )
    role: Literal["user", "system"] | None = Field(
        default=None,
        description=(
            "OAuth only: which role's account the agent uses. Matches "
            "``core.identity.accounts.Role`` — 'user' is the operator's "
            "personal account, 'system' is the daemon-owned one."
        ),
    )
    scopes: list[str] = Field(
        default_factory=list,
        description=(
            "OAuth only: scopes required for this agent to function. "
            "Surfaced in the UI so the operator knows what they're "
            "granting."
        ),
    )
    docs_url: str | None = Field(
        default=None,
        description=(
            "Link to provider docs / where to generate a key. Shown "
            "as a helper link next to the input."
        ),
    )


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
    integrations: list[IntegrationSpec] = Field(
        default_factory=list,
        description=(
            "Per-agent third-party dependencies. Opt-in; agents that "
            "only touch local state leave this empty."
        ),
    )

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_PATTERN.match(v):
            raise ValueError(f"invalid agent name: {v!r}")
        return v

    @field_validator("tools")
    @classmethod
    def _tools(cls, v: list[str]) -> list[str]:
        # agents can never create other agents — only the top-level
        # orchestrator gets agent_create.
        if "agent_create" in v:
            raise ValueError("agent_create cannot appear in an agent's tool list")
        return v

    @classmethod
    def load(cls, path: Path) -> Manifest:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"manifest {path} is not a mapping")
        return cls.model_validate(raw)
