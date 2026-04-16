"""Workspace-scoped filesystem tools.

Both tools reject any path that resolves outside `~/PILK/workspace/`.
There is no absolute-path escape hatch and no symlink following beyond
the workspace root.
"""

from __future__ import annotations

from pathlib import Path

from core.config import get_settings
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 1 * 1024 * 1024


def _workspace_root() -> Path:
    root = get_settings().workspace_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_in_workspace(rel: str) -> Path:
    root = _workspace_root()
    # Disallow absolute paths outright.
    candidate = (root / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as e:
        raise ValueError(f"path escapes workspace: {rel}") from e
    return candidate


async def _fs_read(args: dict, _ctx: ToolContext) -> ToolOutcome:
    path = _resolve_in_workspace(str(args["path"]))
    if not path.exists():
        return ToolOutcome(content=f"not found: {path.name}", is_error=True)
    if not path.is_file():
        return ToolOutcome(content=f"not a file: {path.name}", is_error=True)
    raw = path.read_bytes()
    truncated = len(raw) > MAX_READ_BYTES
    body = raw[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    suffix = f"\n\n[truncated — {len(raw)} bytes, shown {MAX_READ_BYTES}]" if truncated else ""
    return ToolOutcome(
        content=body + suffix,
        data={"bytes": len(raw), "truncated": truncated},
    )


async def _fs_write(args: dict, _ctx: ToolContext) -> ToolOutcome:
    path = _resolve_in_workspace(str(args["path"]))
    content: str = str(args["content"])
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        return ToolOutcome(
            content=f"refused: content too large ({len(data)} > {MAX_WRITE_BYTES})",
            is_error=True,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return ToolOutcome(
        content=f"wrote {len(data)} bytes to {path.relative_to(_workspace_root())}",
        data={"bytes": len(data)},
    )


fs_read_tool = Tool(
    name="fs.read",
    description=(
        "Read a UTF-8 text file from the PILK workspace. Paths are relative "
        "to the workspace root; absolute paths and paths that escape the "
        "workspace are rejected. Large files are truncated to 256 KiB."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path, e.g. 'notes/todo.md'.",
            }
        },
        "required": ["path"],
    },
    risk=RiskClass.READ,
    handler=_fs_read,
)


fs_write_tool = Tool(
    name="fs.write",
    description=(
        "Write a UTF-8 text file to the PILK workspace. Creates parent "
        "directories as needed. Overwrites existing files. Paths must be "
        "workspace-relative; max 1 MiB per write."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path.",
            },
            "content": {
                "type": "string",
                "description": "Full file contents to write.",
            },
        },
        "required": ["path", "content"],
    },
    risk=RiskClass.WRITE_LOCAL,
    handler=_fs_write,
)
