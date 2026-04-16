"""Tool registry.

Tools are registered once at daemon startup. Each tool carries a stable
name, a JSON-Schema input shape, a risk class, and a coroutine to invoke.
The Anthropic tool schemas exposed to the model are derived from this
registry, so there is a single source of truth — no drift between what
Claude sees and what we execute.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.policy.risk import RiskClass


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: RiskClass
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[ToolOutcome]]


@dataclass
class ToolContext:
    plan_id: str | None = None
    step_id: str | None = None
    agent_name: str | None = None
    sandbox_id: str | None = None
    sandbox_root: Path | None = None  # if set, fs/shell tools scope here


@dataclass
class ToolOutcome:
    content: str
    is_error: bool = False
    data: dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def anthropic_schemas(
        self, allow: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """JSON shape the Anthropic API expects in `tools=[...]`.

        If `allow` is provided, only tools in that list are returned — this
        is how an agent restricts the model to its declared tool surface.
        Sorted by name so the rendered byte sequence is stable; otherwise
        the prompt cache would invalidate on every process restart.
        """
        allowed = set(allow) if allow is not None else None
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in sorted(self._tools.values(), key=lambda t: t.name)
            if allowed is None or t.name in allowed
        ]
