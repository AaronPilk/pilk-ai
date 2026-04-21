"""Tests for the new brain_note_search_and_replace tool.

Drives the tool against a real Vault pointed at tmp_path — same
pattern as test_brain_route.py. Covers:
- happy path: replaces all occurrences and persists atomically
- replace_all=false replaces only the first
- empty-replace deletes matches
- missing find string → clear is_error, no write
- non-existent path → FileNotFoundError surfaced as is_error
- empty find string → rejected up-front
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.brain import Vault
from core.policy.risk import RiskClass
from core.tools.builtin.brain import make_brain_tools
from core.tools.registry import ToolContext


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    v = Vault(tmp_path)
    v.ensure_initialized()
    return v


def _get(vault: Vault, name: str):
    for t in make_brain_tools(vault):
        if t.name == name:
            return t
    raise AssertionError(f"no tool named {name}")


def test_tool_registered_with_write_local_risk(vault: Vault) -> None:
    tool = _get(vault, "brain_note_search_and_replace")
    assert tool.risk == RiskClass.WRITE_LOCAL


@pytest.mark.asyncio
async def test_happy_path_replaces_every_occurrence(vault: Vault) -> None:
    (vault.root / "client.md").write_text(
        "Skyway is the main client.\n"
        "Skyway was founded in 2019.\n"
        "Skyway does PPC.\n",
        encoding="utf-8",
    )
    tool = _get(vault, "brain_note_search_and_replace")
    out = await tool.handler(
        {"path": "client.md", "find": "Skyway", "replace": "Skyway Media"},
        ToolContext(),
    )
    assert not out.is_error
    assert out.data["replaced"] == 3
    new_body = (vault.root / "client.md").read_text(encoding="utf-8")
    assert new_body.count("Skyway Media") == 3
    assert "Skyway " not in new_body.replace("Skyway Media", "")


@pytest.mark.asyncio
async def test_replace_all_false_stops_at_first(vault: Vault) -> None:
    (vault.root / "note.md").write_text("foo foo foo\n", encoding="utf-8")
    tool = _get(vault, "brain_note_search_and_replace")
    out = await tool.handler(
        {
            "path": "note.md",
            "find": "foo",
            "replace": "BAR",
            "replace_all": False,
        },
        ToolContext(),
    )
    assert not out.is_error
    assert out.data["replaced"] == 1
    assert (
        (vault.root / "note.md").read_text(encoding="utf-8")
        == "BAR foo foo\n"
    )


@pytest.mark.asyncio
async def test_empty_replace_deletes_match(vault: Vault) -> None:
    (vault.root / "note.md").write_text("keep REMOVE this\n", encoding="utf-8")
    tool = _get(vault, "brain_note_search_and_replace")
    out = await tool.handler(
        {"path": "note.md", "find": "REMOVE ", "replace": ""},
        ToolContext(),
    )
    assert not out.is_error
    assert (
        (vault.root / "note.md").read_text(encoding="utf-8")
        == "keep this\n"
    )


@pytest.mark.asyncio
async def test_missing_find_is_error_not_silent_noop(vault: Vault) -> None:
    """If the target string isn't in the note, the tool must fail
    loudly — silently no-oping would hide bugs in the caller's
    intent (typo in `find`, wrong note path, etc.)."""
    (vault.root / "note.md").write_text("hello world\n", encoding="utf-8")
    tool = _get(vault, "brain_note_search_and_replace")
    out = await tool.handler(
        {"path": "note.md", "find": "missing", "replace": "anything"},
        ToolContext(),
    )
    assert out.is_error
    assert out.data["replaced"] == 0
    # And the note is unchanged.
    assert (
        (vault.root / "note.md").read_text(encoding="utf-8") == "hello world\n"
    )


@pytest.mark.asyncio
async def test_nonexistent_path_returns_error(vault: Vault) -> None:
    tool = _get(vault, "brain_note_search_and_replace")
    out = await tool.handler(
        {"path": "does-not-exist.md", "find": "x", "replace": "y"},
        ToolContext(),
    )
    assert out.is_error
    assert "not found" in out.content.lower()


@pytest.mark.asyncio
async def test_empty_find_is_rejected(vault: Vault) -> None:
    tool = _get(vault, "brain_note_search_and_replace")
    out = await tool.handler(
        {"path": "whatever.md", "find": "", "replace": "x"},
        ToolContext(),
    )
    assert out.is_error
    assert "find" in out.content.lower()


@pytest.mark.asyncio
async def test_missing_path_is_rejected(vault: Vault) -> None:
    tool = _get(vault, "brain_note_search_and_replace")
    out = await tool.handler(
        {"find": "x", "replace": "y"}, ToolContext(),
    )
    assert out.is_error
    assert "path" in out.content.lower()
