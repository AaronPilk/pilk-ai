"""Sandbox contract.

Every sandbox driver (process today, browser and VM/remote later)
implements this interface. The manager treats sandboxes uniformly: it
knows how to create, describe, and destroy them — it doesn't know or
care what's inside.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class SandboxDescription:
    id: str
    type: str
    agent_name: str | None
    profile: str
    workspace: Path
    state: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "agent_name": self.agent_name,
            "profile": self.profile,
            "workspace": str(self.workspace),
            "state": self.state,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }


@runtime_checkable
class Sandbox(Protocol):
    description: SandboxDescription

    async def ensure(self) -> None:
        """Create on-disk state if needed. Idempotent."""

    async def destroy(self) -> None:
        """Tear down the sandbox. After this it is unusable."""

    async def health(self) -> dict[str, Any]:
        """Return a small status dict for the dashboard."""
