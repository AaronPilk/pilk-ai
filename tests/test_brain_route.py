"""HTTP tests for the /brain route — list, read, search.

We don't spin up the full FastAPI app; we drive the handlers directly
with a stubbed Request whose ``app.state.brain`` is a real Vault
pointed at a tmp_path. That keeps the tests fast and lets us assert on
the exact JSON shape the dashboard consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import HTTPException

from core.api.routes import brain as brain_route
from core.brain import Vault


@dataclass
class _FakeState:
    brain: Vault | None


class _FakeApp:
    def __init__(self, brain: Vault | None) -> None:
        self.state = _FakeState(brain=brain)


class _FakeRequest:
    def __init__(self, brain: Vault | None) -> None:
        self.app = _FakeApp(brain)


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    v = Vault(tmp_path)
    v.ensure_initialized()
    # Give the vault a couple of real notes for list / read / search.
    (tmp_path / "daily").mkdir(parents=True, exist_ok=True)
    (tmp_path / "daily" / "2026-04-20.md").write_text(
        "# 2026-04-20\n\nShipped the UGC scout agent.\n"
        "See [[PILK architecture]] for the puppet-master framing.\n",
        encoding="utf-8",
    )
    (tmp_path / "PILK architecture.md").write_text(
        "# PILK architecture\n\nThe operator is the master of the "
        "puppet master.\n",
        encoding="utf-8",
    )
    return v


# ── list ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_every_note(vault: Vault) -> None:
    r = await brain_route.list_notes(_FakeRequest(vault))
    paths = {n["path"] for n in r["notes"]}
    # The starter README is seeded by ensure_initialized even when we
    # add our own notes, so we only assert that our two notes are
    # present (whether README is there or not is a Vault-level
    # concern).
    assert "daily/2026-04-20.md" in paths
    assert "PILK architecture.md" in paths
    assert r["root"] == str(vault.root)


@pytest.mark.asyncio
async def test_list_rows_carry_folder_stem_and_size(vault: Vault) -> None:
    r = await brain_route.list_notes(_FakeRequest(vault))
    by_path = {n["path"]: n for n in r["notes"]}
    daily = by_path["daily/2026-04-20.md"]
    assert daily["folder"] == "daily"
    assert daily["stem"] == "2026-04-20"
    assert daily["size"] > 0
    assert daily["mtime"] is not None


@pytest.mark.asyncio
async def test_list_503_when_vault_missing() -> None:
    with pytest.raises(HTTPException) as exc:
        await brain_route.list_notes(_FakeRequest(None))
    assert exc.value.status_code == 503


# ── read ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_returns_body(vault: Vault) -> None:
    r = await brain_route.read_note(
        _FakeRequest(vault), path="daily/2026-04-20.md"
    )
    assert r["path"] == "daily/2026-04-20.md"
    assert "Shipped the UGC scout agent." in r["body"]
    assert r["size"] > 0


@pytest.mark.asyncio
async def test_read_accepts_path_without_md_suffix(vault: Vault) -> None:
    """The vault's resolver appends .md; the route should honour
    that so clients can pass a tidy display path."""
    r = await brain_route.read_note(
        _FakeRequest(vault), path="PILK architecture"
    )
    assert r["path"] == "PILK architecture.md"
    assert "puppet master" in r["body"]


@pytest.mark.asyncio
async def test_read_404_when_missing(vault: Vault) -> None:
    with pytest.raises(HTTPException) as exc:
        await brain_route.read_note(
            _FakeRequest(vault), path="does-not-exist"
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_read_400_on_path_escape(vault: Vault) -> None:
    with pytest.raises(HTTPException) as exc:
        await brain_route.read_note(
            _FakeRequest(vault), path="../outside.md"
        )
    assert exc.value.status_code == 400


# ── search ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_finds_substring(vault: Vault) -> None:
    r = await brain_route.search_notes(
        _FakeRequest(vault), q="puppet", limit=50
    )
    assert r["query"] == "puppet"
    paths = {h["path"] for h in r["hits"]}
    # The phrase appears in both notes (daily journal + architecture).
    assert "PILK architecture.md" in paths


@pytest.mark.asyncio
async def test_search_no_hits_returns_empty_list(vault: Vault) -> None:
    r = await brain_route.search_notes(
        _FakeRequest(vault), q="unicorn", limit=50
    )
    assert r["hits"] == []


@pytest.mark.asyncio
async def test_search_hits_carry_line_and_snippet(vault: Vault) -> None:
    r = await brain_route.search_notes(
        _FakeRequest(vault), q="UGC scout", limit=50
    )
    assert r["hits"], "expected at least one hit"
    hit = r["hits"][0]
    assert hit["line"] >= 1
    assert "UGC scout" in hit["snippet"]


@pytest.mark.asyncio
async def test_search_503_when_vault_missing() -> None:
    with pytest.raises(HTTPException) as exc:
        await brain_route.search_notes(
            _FakeRequest(None), q="anything", limit=50
        )
    assert exc.value.status_code == 503
