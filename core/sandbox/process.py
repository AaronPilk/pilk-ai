"""Process sandbox.

The simplest sandbox we ship: a per-agent workspace directory under
`~/PILK/sandboxes/{id}/workspace/` that the fs and shell tools will be
constrained to. The "process" in the name reflects that subprocesses
launched by shell_exec will run with cwd inside this dir; the sandbox
itself is lightweight — no long-running child process of its own.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.sandbox.base import SandboxDescription


class ProcessSandbox:
    def __init__(
        self,
        *,
        sandbox_id: str,
        agent_name: str | None,
        profile: str,
        root: Path,
        capabilities: frozenset[str] = frozenset(),
    ) -> None:
        self._root = root
        self.description = SandboxDescription(
            id=sandbox_id,
            type="process",
            agent_name=agent_name,
            profile=profile,
            workspace=root / "workspace",
            state="creating",
            created_at=datetime.now(UTC).isoformat(),
            capabilities=capabilities,
        )

    @property
    def workspace(self) -> Path:
        return self.description.workspace

    async def ensure(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.description.state = "ready"

    async def destroy(self) -> None:
        # We do not wipe the workspace on destroy — the agent's files may
        # be referenced later and are easy for the user to inspect/clean
        # manually under ~/PILK/sandboxes/. A future GC pass can prune.
        self.description.state = "destroyed"

    async def health(self) -> dict[str, Any]:
        exists = self.workspace.exists()
        return {
            "ok": exists and self.description.state == "ready",
            "workspace_exists": exists,
            "state": self.description.state,
        }
