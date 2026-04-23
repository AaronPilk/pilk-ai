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


# ── graph ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_graph_emits_node_per_note(vault: Vault) -> None:
    r = await brain_route.graph(_FakeRequest(vault))
    node_ids = {n["id"] for n in r["nodes"]}
    assert "daily/2026-04-20.md" in node_ids
    assert "PILK architecture.md" in node_ids
    # Nodes carry folder + size for the UI's cluster colouring.
    by_id = {n["id"]: n for n in r["nodes"]}
    assert by_id["daily/2026-04-20.md"]["folder"] == "daily"
    assert by_id["PILK architecture.md"]["size"] > 0


@pytest.mark.asyncio
async def test_graph_resolves_wikilink_by_stem(vault: Vault) -> None:
    r = await brain_route.graph(_FakeRequest(vault))
    edges = {(e["source"], e["target"]) for e in r["edges"]}
    # The daily note links to [[PILK architecture]] — the resolver
    # matches the stem and produces an edge.
    assert (
        "daily/2026-04-20.md",
        "PILK architecture.md",
    ) in edges


@pytest.mark.asyncio
async def test_graph_drops_unresolvable_links(tmp_path: Path) -> None:
    v = Vault(tmp_path)
    v.ensure_initialized()
    (tmp_path / "only.md").write_text(
        "# only\n\nTalks about [[ghost]] and [[another-ghost]].\n",
        encoding="utf-8",
    )
    r = await brain_route.graph(_FakeRequest(v))
    # One node (plus whatever ensure_initialized seeded); no edges.
    assert any(n["id"] == "only.md" for n in r["nodes"])
    assert all(e["source"] != "only.md" for e in r["edges"])


@pytest.mark.asyncio
async def test_graph_resolves_folder_prefixed_links(tmp_path: Path) -> None:
    v = Vault(tmp_path)
    v.ensure_initialized()
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "a.md").write_text("# a\n\nSee [[beta/b]].\n")
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta" / "b.md").write_text("# b\n")
    r = await brain_route.graph(_FakeRequest(v))
    edges = {(e["source"], e["target"]) for e in r["edges"]}
    assert ("alpha/a.md", "beta/b.md") in edges


@pytest.mark.asyncio
async def test_graph_dedupes_repeated_wikilinks(tmp_path: Path) -> None:
    v = Vault(tmp_path)
    v.ensure_initialized()
    (tmp_path / "hub.md").write_text(
        "# hub\n\n[[spoke]] [[spoke]] [[spoke|display]]\n",
        encoding="utf-8",
    )
    (tmp_path / "spoke.md").write_text("# spoke\n")
    r = await brain_route.graph(_FakeRequest(v))
    count = sum(
        1
        for e in r["edges"]
        if e["source"] == "hub.md" and e["target"] == "spoke.md"
    )
    assert count == 1


@pytest.mark.asyncio
async def test_graph_ignores_self_links(tmp_path: Path) -> None:
    v = Vault(tmp_path)
    v.ensure_initialized()
    (tmp_path / "lonely.md").write_text(
        "# lonely\n\nRefers to [[lonely]] for no good reason.\n",
        encoding="utf-8",
    )
    r = await brain_route.graph(_FakeRequest(v))
    assert not any(
        e["source"] == e["target"] for e in r["edges"]
    )


@pytest.mark.asyncio
async def test_graph_503_when_vault_missing() -> None:
    with pytest.raises(HTTPException) as exc:
        await brain_route.graph(_FakeRequest(None))
    assert exc.value.status_code == 503


# ── backlinks ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backlinks_returns_linking_notes(vault: Vault) -> None:
    r = await brain_route.backlinks(
        _FakeRequest(vault), path="PILK architecture"
    )
    assert r["target"] == "PILK architecture.md"
    paths = [link["path"] for link in r["links"]]
    assert "daily/2026-04-20.md" in paths


@pytest.mark.asyncio
async def test_backlinks_empty_when_nothing_links(tmp_path: Path) -> None:
    v = Vault(tmp_path)
    v.ensure_initialized()
    (tmp_path / "solo.md").write_text("# solo\n\nStanding alone.\n")
    r = await brain_route.backlinks(_FakeRequest(v), path="solo.md")
    assert r["links"] == []


@pytest.mark.asyncio
async def test_backlinks_404_when_target_missing(vault: Vault) -> None:
    with pytest.raises(HTTPException) as exc:
        await brain_route.backlinks(
            _FakeRequest(vault), path="no-such-note.md"
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_backlinks_503_when_vault_missing() -> None:
    with pytest.raises(HTTPException) as exc:
        await brain_route.backlinks(
            _FakeRequest(None), path="anything.md"
        )
    assert exc.value.status_code == 503


# ── CRM categories + paginated notes + detail ──────────────────────


@pytest.fixture
def crm_vault(tmp_path: Path) -> Vault:
    """Seed a vault with notes in every CRM category so the listing
    endpoints have realistic data to slice."""
    v = Vault(tmp_path)
    v.ensure_initialized()
    fixtures = {
        "ingested/gmail/thread-1.md": "# Inbox thread\n\nUnread email.\n",
        "ingested/chatgpt/2024-01-05-gold.md": "# Gold scalp\n\nXAUUSD notes.\n",
        "clients/skyway/offer-a.md": "# Offer A\n\nClient-facing brief.\n",
        "sessions/2026-04-20.md": "# Session log\n\nDaily session.\n",
        "daily/2026-04-21.md": "# Daily\n\nToday's log.\n",
        "tg-telegram-notes.md": "# Telegram log\n\nChat transcript.\n",
        "trading/xauusd-plan.md": "# XAUUSD plan\n\nTrade setup.\n",
        "xauusd/position-log.md": "# XAUUSD positions\n\nPosition log.\n",
        "ingested/uploads/sales-ops/script.md": "# Sales script\n\nOutbound copy.\n",
        "ingested/uploads/projects/roadmap.md": "# Roadmap\n\nProject plan.\n",
        "random-uncategorised.md": "# Random\n\nA note with no home.\n",
    }
    for rel, body in fixtures.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return v


@pytest.mark.asyncio
async def test_list_categories_returns_counts(crm_vault: Vault) -> None:
    r = await brain_route.list_categories(_FakeRequest(crm_vault))
    by_id = {c["id"]: c for c in r["categories"]}
    # Each bespoke category has at least one matching fixture.
    assert by_id["inbox"]["count"] >= 1
    assert by_id["chat_archive"]["count"] >= 1
    assert by_id["clients"]["count"] >= 1
    assert by_id["trading"]["count"] >= 2  # trading/ + xauusd/
    assert by_id["sales_ops"]["count"] >= 1
    assert by_id["projects"]["count"] >= 1
    assert by_id["personal"]["count"] >= 3  # sessions, daily, tg-
    # All Notes count >= the individual category count.
    assert by_id["all"]["count"] >= by_id["inbox"]["count"]
    assert r["page_size"] == brain_route.NOTES_PAGE_SIZE


def test_classify_category_rules() -> None:
    assert brain_route._classify_category("ingested/gmail/x.md") == "inbox"
    assert brain_route._classify_category("ingested/chatgpt/x.md") == "chat_archive"
    assert brain_route._classify_category("clients/acme/brief.md") == "clients"
    assert brain_route._classify_category("trading/plan.md") == "trading"
    assert brain_route._classify_category("xauusd/positions.md") == "trading"
    assert brain_route._classify_category("sessions/log.md") == "personal"
    assert brain_route._classify_category("daily/2026-04-20.md") == "personal"
    assert brain_route._classify_category("tg-telegram.md") == "personal"
    assert brain_route._classify_category(
        "ingested/uploads/sales-ops/script.md",
    ) == "sales_ops"
    assert brain_route._classify_category(
        "ingested/uploads/projects/plan.md",
    ) == "projects"
    assert brain_route._classify_category("random.md") == "all"


@pytest.mark.asyncio
async def test_list_notes_paginated_filters_by_category(
    crm_vault: Vault,
) -> None:
    r = await brain_route.list_notes_paginated(
        _FakeRequest(crm_vault), category="trading", page=1,
        page_size=10, q="",
    )
    paths = [n["path"] for n in r["notes"]]
    assert all(
        p.startswith("trading/") or p.startswith("xauusd/")
        for p in paths
    ), paths
    assert r["category"] == "trading"
    assert r["page"] == 1
    assert r["total"] == len(paths)


@pytest.mark.asyncio
async def test_list_notes_paginated_pagination_math(
    crm_vault: Vault,
) -> None:
    r = await brain_route.list_notes_paginated(
        _FakeRequest(crm_vault), category="all", page=1,
        page_size=3, q="",
    )
    assert r["page_size"] == 3
    assert len(r["notes"]) <= 3
    assert r["pages"] >= 1
    # total across all pages matches actual vault size (>= the fixture
    # count — the Vault seeds a starter README).
    assert r["total"] >= 11


@pytest.mark.asyncio
async def test_list_notes_paginated_query_filter(
    crm_vault: Vault,
) -> None:
    r = await brain_route.list_notes_paginated(
        _FakeRequest(crm_vault), category="all", page=1,
        page_size=50, q="xauusd",
    )
    paths = [n["path"] for n in r["notes"]]
    # 'xauusd' appears in trading/xauusd-plan.md + xauusd/position-log.md.
    assert any("xauusd" in p.lower() for p in paths)
    for p in paths:
        # Every result must contain the needle somewhere visible.
        assert "xauusd" in p.lower() or "xauusd" in (
            (_row_title(r["notes"], p) or "").lower()
        )


def _row_title(rows: list, path: str) -> str | None:
    for row in rows:
        if row["path"] == path:
            return row.get("title") or row.get("stem")
    return None


@pytest.mark.asyncio
async def test_read_note_with_backlinks_returns_body_and_category(
    crm_vault: Vault,
) -> None:
    r = await brain_route.read_note_with_backlinks(
        _FakeRequest(crm_vault), path="clients/skyway/offer-a.md",
    )
    assert r["path"] == "clients/skyway/offer-a.md"
    assert "Offer A" in r["body"]
    assert r["category"] == "clients"
    assert "note" in r
    assert isinstance(r["backlinks"], list)


@pytest.mark.asyncio
async def test_read_note_with_backlinks_404s_on_missing(
    crm_vault: Vault,
) -> None:
    with pytest.raises(HTTPException) as exc:
        await brain_route.read_note_with_backlinks(
            _FakeRequest(crm_vault), path="does/not/exist.md",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_categories_requires_vault() -> None:
    with pytest.raises(HTTPException) as exc:
        await brain_route.list_categories(_FakeRequest(None))
    assert exc.value.status_code == 503
