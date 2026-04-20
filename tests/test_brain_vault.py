"""Tests for the Obsidian-compatible brain vault + its 4 PILK tools.

All tests run against a tmp_path vault; no network and no real
Obsidian process.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.brain import Vault
from core.tools.builtin.brain import make_brain_tools
from core.tools.registry import ToolContext


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    v = Vault(tmp_path / "brain")
    v.ensure_initialized()
    return v


# ── Vault-level tests ───────────────────────────────────────────


def test_ensure_initialized_seeds_starter(tmp_path: Path) -> None:
    v = Vault(tmp_path / "brain")
    v.ensure_initialized()
    readme = (tmp_path / "brain" / "README.md").read_text()
    assert "PILK brain" in readme


def test_ensure_initialized_preserves_existing(tmp_path: Path) -> None:
    """If the vault already has notes, don't overwrite anything."""
    root = tmp_path / "brain"
    root.mkdir(parents=True, exist_ok=True)
    (root / "my-note.md").write_text("# Mine", encoding="utf-8")
    v = Vault(root)
    v.ensure_initialized()
    assert (root / "my-note.md").read_text() == "# Mine"
    assert not (root / "README.md").exists()


def test_resolve_rejects_absolute_paths(vault: Vault) -> None:
    with pytest.raises(ValueError):
        vault.resolve("/etc/passwd")


def test_resolve_rejects_parent_escape(vault: Vault) -> None:
    with pytest.raises(ValueError):
        vault.resolve("../../secrets")


def test_resolve_appends_md_extension(vault: Vault) -> None:
    p = vault.resolve("notes/working-style")
    assert p.name == "working-style.md"


def test_write_then_read_roundtrip(vault: Vault) -> None:
    vault.write("research/ads.md", "# Ads research\n\n[[Aaron]] runs Skyway.")
    body = vault.read("research/ads.md")
    assert "Skyway" in body


def test_write_append(vault: Vault) -> None:
    vault.write("log.md", "one")
    vault.write("log.md", "two", append=True)
    assert "one" in vault.read("log.md")
    assert "two" in vault.read("log.md")


def test_search_finds_hits(vault: Vault) -> None:
    vault.write("a.md", "Aaron runs Skyway Media")
    vault.write("b.md", "A follow-up about kittens")
    hits = vault.search("skyway")
    assert len(hits) == 1
    assert hits[0].path == "a.md"
    assert "Skyway" in hits[0].snippet


def test_list_paths(vault: Vault) -> None:
    vault.write("alpha.md", "x")
    vault.write("nested/beta.md", "y")
    paths = vault.list()
    # Starter README + the two we added
    assert "README.md" in paths
    assert "alpha.md" in paths
    assert "nested/beta.md" in paths


# ── Tool-level tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_brain_note_write_and_read_roundtrip(vault: Vault) -> None:
    tools = {t.name: t for t in make_brain_tools(vault)}
    out = await tools["brain_note_write"].handler(
        {"path": "notes/one.md", "content": "hello brain"},
        ToolContext(),
    )
    assert not out.is_error
    assert "saved" in out.content.lower()

    out2 = await tools["brain_note_read"].handler(
        {"path": "notes/one.md"}, ToolContext()
    )
    assert not out2.is_error
    assert "hello brain" in out2.content


@pytest.mark.asyncio
async def test_brain_note_write_rejects_empty_content(vault: Vault) -> None:
    tools = {t.name: t for t in make_brain_tools(vault)}
    out = await tools["brain_note_write"].handler(
        {"path": "blank.md", "content": ""}, ToolContext()
    )
    assert out.is_error
    assert "content" in out.content.lower()


@pytest.mark.asyncio
async def test_brain_note_read_missing(vault: Vault) -> None:
    tools = {t.name: t for t in make_brain_tools(vault)}
    out = await tools["brain_note_read"].handler(
        {"path": "nope.md"}, ToolContext()
    )
    assert out.is_error
    assert "not found" in out.content.lower()


@pytest.mark.asyncio
async def test_brain_search_hits(vault: Vault) -> None:
    vault.write("foo.md", "Tampa Bay Rays")
    tools = {t.name: t for t in make_brain_tools(vault)}
    out = await tools["brain_search"].handler(
        {"query": "tampa"}, ToolContext()
    )
    assert not out.is_error
    assert "foo.md" in out.content


@pytest.mark.asyncio
async def test_brain_search_empty_query_rejected(vault: Vault) -> None:
    tools = {t.name: t for t in make_brain_tools(vault)}
    out = await tools["brain_search"].handler(
        {"query": ""}, ToolContext()
    )
    assert out.is_error


@pytest.mark.asyncio
async def test_brain_note_list(vault: Vault) -> None:
    vault.write("a.md", "x")
    vault.write("projects/b.md", "y")
    tools = {t.name: t for t in make_brain_tools(vault)}
    out = await tools["brain_note_list"].handler({}, ToolContext())
    assert not out.is_error
    assert "a.md" in out.content
    assert "projects/b.md" in out.content


@pytest.mark.asyncio
async def test_brain_note_list_scoped_folder(vault: Vault) -> None:
    vault.write("a.md", "x")
    vault.write("projects/b.md", "y")
    tools = {t.name: t for t in make_brain_tools(vault)}
    out = await tools["brain_note_list"].handler(
        {"folder": "projects"}, ToolContext()
    )
    assert not out.is_error
    assert "projects/b.md" in out.content
    assert "a.md" not in out.content
