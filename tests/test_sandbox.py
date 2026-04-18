from pathlib import Path

import pytest

from core.config import get_settings
from core.db import ensure_schema
from core.sandbox import ProcessSandbox, SandboxManager
from core.tools.builtin import fs_read_tool, fs_write_tool
from core.tools.registry import ToolContext


@pytest.mark.asyncio
async def test_process_sandbox_creates_workspace() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    mgr = SandboxManager(
        sandboxes_dir=settings.sandboxes_dir, db_path=settings.db_path
    )
    sb = await mgr.get_or_create(
        type="process", agent_name="a1", profile="p1"
    )
    assert isinstance(sb, ProcessSandbox)
    assert sb.description.state == "ready"
    assert sb.workspace.exists()
    assert "a1" in sb.description.id


@pytest.mark.asyncio
async def test_sandbox_idempotent() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    mgr = SandboxManager(
        sandboxes_dir=settings.sandboxes_dir, db_path=settings.db_path
    )
    a = await mgr.get_or_create(type="process", agent_name="foo", profile="default")
    b = await mgr.get_or_create(type="process", agent_name="foo", profile="default")
    assert a is b


@pytest.mark.asyncio
async def test_fs_tools_honor_sandbox_root(tmp_path: Path) -> None:
    sandbox_root = tmp_path / "sb"
    sandbox_root.mkdir()
    ctx = ToolContext(sandbox_root=sandbox_root)

    out = await fs_write_tool.handler(
        {"path": "hello.txt", "content": "sandboxed"}, ctx
    )
    assert not out.is_error
    assert (sandbox_root / "hello.txt").read_text() == "sandboxed"

    read = await fs_read_tool.handler({"path": "hello.txt"}, ctx)
    assert not read.is_error
    assert read.content.startswith("sandboxed")


@pytest.mark.asyncio
async def test_sandbox_scope_still_rejects_escape(tmp_path: Path) -> None:
    sandbox_root = tmp_path / "sb"
    sandbox_root.mkdir()
    ctx = ToolContext(sandbox_root=sandbox_root)
    with pytest.raises(ValueError, match="escapes workspace"):
        await fs_write_tool.handler(
            {"path": "../escape.txt", "content": "nope"}, ctx
        )
