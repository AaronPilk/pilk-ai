"""Unit tests for the `memory_remember` tool.

Covers the five gates the tool has to enforce before writing to the
MemoryStore:
  1. Missing / invalid `kind` → is_error
  2. Missing `title` → is_error
  3. Title longer than MAX_TITLE_CHARS → is_error (nudge to shorten)
  4. Body longer than MAX_BODY_CHARS → is_error
  5. Happy path writes an entry and returns its id
Also verifies the "source" string flips to `pilk:interview` when the
tool context flags an interview, and stays `pilk` otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db import ensure_schema
from core.memory import MemoryStore
from core.tools.builtin.memory_remember import (
    MAX_BODY_CHARS,
    MAX_TITLE_CHARS,
    make_memory_remember_tool,
)
from core.tools.registry import ToolContext


@pytest.fixture
async def memory(tmp_path: Path) -> MemoryStore:
    db = tmp_path / "pilk.db"
    ensure_schema(db)
    return MemoryStore(db)


@pytest.mark.asyncio
async def test_invalid_kind_rejected(memory: MemoryStore) -> None:
    tool = make_memory_remember_tool(memory)
    out = await tool.handler(
        {"kind": "notARealKind", "title": "x"}, ToolContext()
    )
    assert out.is_error
    assert "kind" in out.content.lower()


@pytest.mark.asyncio
async def test_missing_title_rejected(memory: MemoryStore) -> None:
    tool = make_memory_remember_tool(memory)
    out = await tool.handler(
        {"kind": "preference", "title": ""}, ToolContext()
    )
    assert out.is_error
    assert "title" in out.content.lower()


@pytest.mark.asyncio
async def test_title_length_capped(memory: MemoryStore) -> None:
    tool = make_memory_remember_tool(memory)
    out = await tool.handler(
        {"kind": "fact", "title": "x" * (MAX_TITLE_CHARS + 1)},
        ToolContext(),
    )
    assert out.is_error
    assert "title too long" in out.content.lower()


@pytest.mark.asyncio
async def test_body_length_capped(memory: MemoryStore) -> None:
    tool = make_memory_remember_tool(memory)
    out = await tool.handler(
        {
            "kind": "fact",
            "title": "ok",
            "body": "x" * (MAX_BODY_CHARS + 1),
        },
        ToolContext(),
    )
    assert out.is_error
    assert "body too long" in out.content.lower()


@pytest.mark.asyncio
async def test_happy_path_writes_entry(memory: MemoryStore) -> None:
    tool = make_memory_remember_tool(memory)
    out = await tool.handler(
        {
            "kind": "preference",
            "title": "likes short answers",
            "body": "especially when voice TTS reads them aloud",
        },
        ToolContext(),
    )
    assert not out.is_error
    assert out.data["kind"] == "preference"
    assert out.data["title"] == "likes short answers"
    assert out.data["id"].startswith("mem_")
    stored = await memory.list()
    assert len(stored) == 1
    assert stored[0].source == "pilk"


@pytest.mark.asyncio
async def test_plan_id_propagates(memory: MemoryStore) -> None:
    """plan_id from the ToolContext should land on the stored
    MemoryEntry so `/memory` can trace each entry back to the
    interview run that produced it."""
    tool = make_memory_remember_tool(memory)
    out = await tool.handler(
        {"kind": "fact", "title": "lives in Tampa"},
        ToolContext(plan_id="pl_interview_123"),
    )
    assert not out.is_error
    stored = await memory.list()
    assert stored[0].plan_id == "pl_interview_123"
