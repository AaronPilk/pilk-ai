import pytest

from core.config import get_settings
from core.tools.builtin import fs_read_tool, fs_write_tool
from core.tools.registry import ToolContext


@pytest.mark.asyncio
async def test_write_then_read_roundtrip() -> None:
    ctx = ToolContext()
    write = await fs_write_tool.handler(
        {"path": "notes/hello.txt", "content": "hello PILK"}, ctx
    )
    assert not write.is_error
    assert write.data["bytes"] == len(b"hello PILK")

    read = await fs_read_tool.handler({"path": "notes/hello.txt"}, ctx)
    assert not read.is_error
    assert read.content.startswith("hello PILK")


@pytest.mark.asyncio
async def test_write_refuses_workspace_escape() -> None:
    with pytest.raises(ValueError, match="escapes workspace"):
        await fs_write_tool.handler(
            {"path": "../escape.txt", "content": "nope"}, ToolContext()
        )


@pytest.mark.asyncio
async def test_read_missing_file_returns_error() -> None:
    result = await fs_read_tool.handler({"path": "does/not/exist.txt"}, ToolContext())
    assert result.is_error
    assert "not found" in result.content.lower()


@pytest.mark.asyncio
async def test_absolute_path_outside_workspace_is_rejected() -> None:
    with pytest.raises(ValueError, match="escapes workspace"):
        await fs_read_tool.handler({"path": "/etc/passwd"}, ToolContext())


@pytest.mark.asyncio
async def test_fixture_points_workspace_inside_tmp_home() -> None:
    # Sanity check the conftest isolation.
    root = get_settings().workspace_dir.expanduser().resolve()
    assert "pilk-test" in str(root) or str(root).endswith("/workspace")
