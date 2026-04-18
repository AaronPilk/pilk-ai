"""Coding engine protocol + shared value types.

Kept deliberately small so another engine can be written in one file.
Engines are stateless across runs; configuration lives on the instance
(API client, bridge URL, etc.) and is injected by `app.py` at boot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

CodeScope = Literal["function", "file", "repo"]


@dataclass
class CodeTask:
    """One unit of work handed to a coding engine.

    `scope` gates routing — repo-scope work prefers Claude Code / Agent
    SDK; function/file-scope work is fine on the bare API.
    """

    goal: str
    scope: CodeScope = "function"
    repo_path: Path | None = None
    prefer_engine: str | None = None   # user override ("claude-code" | "agent-sdk" | "api")
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class CodeRunResult:
    """What the engine produces for the orchestrator + ledger."""

    engine: str                # "claude-code" | "agent-sdk" | "api"
    ok: bool
    summary: str               # one human-readable sentence
    detail: str = ""           # multi-line extra detail (prose, diffs, etc.)
    usd: float = 0.0           # 0 for subscription-based engines (Claude Code)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EngineHealth:
    """Cheap status snapshot for the Coding engines settings card."""

    name: str
    available: bool
    label: str                 # human-readable display name
    detail: str = ""           # one-line reason when unavailable


@runtime_checkable
class CodingEngine(Protocol):
    """Protocol every coding backend satisfies.

    `available()` is called every time the router needs to decide, so
    it must be cheap and never throw. A health probe that talks to a
    local process is fine; anything involving the public internet is
    not.
    """

    name: str
    label: str

    async def available(self) -> bool: ...

    async def health(self) -> EngineHealth: ...

    async def run(self, task: CodeTask) -> CodeRunResult: ...
