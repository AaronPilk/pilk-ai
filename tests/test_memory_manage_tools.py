"""Unit tests for memory_list + memory_delete.

Mirrors the memory_remember test shape. Both tools run against a real
SQLite-backed MemoryStore so the SQL path is exercised end-to-end —
the tool surface is thin enough that mocking the store would test
almost nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db import ensure_schema
from core.memory import MemoryStore
from core.tools.builtin.memory_manage import (
    MAX_LIST_RETURN,
    make_memory_delete_tool,
    make_memory_list_tool,
)
from core.tools.registry import ToolContext


@pytest.fixture
async def memory(tmp_path: Path) -> MemoryStore:
    db = tmp_path / "pilk.db"
    ensure_schema(db)
    return MemoryStore(db)


# ── memory_list ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_empty_returns_hint(memory: MemoryStore) -> None:
    tool = make_memory_list_tool(memory)
    out = await tool.handler({}, ToolContext())
    assert not out.is_error
    assert out.data["entries"] == []
    assert "memory_remember" in out.content


@pytest.mark.asyncio
async def test_list_returns_every_kind(memory: MemoryStore) -> None:
    await memory.add(kind="preference", title="short replies", body="")
    await memory.add(kind="fact", title="lives in Tampa", body="")
    tool = make_memory_list_tool(memory)
    out = await tool.handler({}, ToolContext())
    assert not out.is_error
    assert len(out.data["entries"]) == 2
    # Includes kind + title in content.
    assert "preference" in out.content
    assert "fact" in out.content


@pytest.mark.asyncio
async def test_list_filters_by_kind(memory: MemoryStore) -> None:
    await memory.add(kind="preference", title="short replies", body="")
    await memory.add(kind="fact", title="lives in Tampa", body="")
    tool = make_memory_list_tool(memory)
    out = await tool.handler({"kind": "fact"}, ToolContext())
    assert len(out.data["entries"]) == 1
    assert out.data["entries"][0]["title"] == "lives in Tampa"


@pytest.mark.asyncio
async def test_list_rejects_unknown_kind(memory: MemoryStore) -> None:
    tool = make_memory_list_tool(memory)
    out = await tool.handler({"kind": "notReal"}, ToolContext())
    assert out.is_error
    assert "kind" in out.content.lower()


@pytest.mark.asyncio
async def test_list_truncates_at_cap(memory: MemoryStore) -> None:
    """Guard the max-return cap so a pathological number of entries
    doesn't flood a planner turn."""
    for i in range(MAX_LIST_RETURN + 5):
        await memory.add(kind="fact", title=f"entry {i}", body="")
    tool = make_memory_list_tool(memory)
    out = await tool.handler({}, ToolContext())
    assert len(out.data["entries"]) == MAX_LIST_RETURN
    assert out.data["total"] == MAX_LIST_RETURN + 5
    assert "showing first" in out.content.lower()


# ── memory_delete ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_requires_id_or_title(memory: MemoryStore) -> None:
    tool = make_memory_delete_tool(memory)
    out = await tool.handler({}, ToolContext())
    assert out.is_error
    assert "id" in out.content.lower() or "title" in out.content.lower()


@pytest.mark.asyncio
async def test_delete_by_id(memory: MemoryStore) -> None:
    entry = await memory.add(kind="fact", title="something", body="")
    tool = make_memory_delete_tool(memory)
    out = await tool.handler({"id": entry.id}, ToolContext())
    assert not out.is_error
    assert out.data["deleted"] is True
    assert await memory.list() == []


@pytest.mark.asyncio
async def test_delete_unknown_id_returns_error(memory: MemoryStore) -> None:
    tool = make_memory_delete_tool(memory)
    out = await tool.handler({"id": "mem_ghost"}, ToolContext())
    assert out.is_error
    assert "mem_ghost" in out.content


@pytest.mark.asyncio
async def test_delete_by_title_exact(memory: MemoryStore) -> None:
    await memory.add(kind="preference", title="short replies", body="")
    await memory.add(kind="fact", title="lives in Tampa", body="")
    tool = make_memory_delete_tool(memory)
    out = await tool.handler({"title": "lives in Tampa"}, ToolContext())
    assert not out.is_error
    remaining = await memory.list()
    assert len(remaining) == 1
    assert remaining[0].title == "short replies"


@pytest.mark.asyncio
async def test_delete_by_title_no_match(memory: MemoryStore) -> None:
    await memory.add(kind="fact", title="real title", body="")
    tool = make_memory_delete_tool(memory)
    out = await tool.handler({"title": "not a title"}, ToolContext())
    assert out.is_error
    assert "not a title" in out.content


@pytest.mark.asyncio
async def test_delete_by_title_duplicates_picks_most_recent(
    memory: MemoryStore,
) -> None:
    """Two entries with the same title: delete the most recent one
    (matches MemoryStore.list() DESC-by-created_at ordering)."""
    older = await memory.add(kind="fact", title="dup", body="first")
    newer = await memory.add(kind="fact", title="dup", body="second")
    tool = make_memory_delete_tool(memory)
    out = await tool.handler({"title": "dup"}, ToolContext())
    assert not out.is_error
    remaining = await memory.list()
    assert len(remaining) == 1
    assert remaining[0].id == older.id
    assert newer.id not in {r.id for r in remaining}
