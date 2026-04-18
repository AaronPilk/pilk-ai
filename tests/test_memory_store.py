"""MemoryStore — CRUD + kind filter + clear."""

from __future__ import annotations

import pytest

from core.config import get_settings
from core.db import ensure_schema
from core.memory import MemoryStore


@pytest.mark.asyncio
async def test_add_list_delete_clear() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    store = MemoryStore(settings.db_path)

    # Empty on fresh schema.
    assert await store.list() == []

    pref = await store.add(
        kind="preference",
        title="No meetings before 11am",
        body="Protect deep work in the morning.",
    )
    fact = await store.add(
        kind="fact",
        title="Wife's birthday is August 12",
        body="",
    )
    pat = await store.add(
        kind="pattern",
        title="Clears inbox on Friday afternoons",
        body="Batch-processes email between 3pm and 5pm.",
    )

    # list returns newest-first
    all_entries = await store.list()
    assert [e.id for e in all_entries] == [pat.id, fact.id, pref.id]

    # kind filter
    facts = await store.list(kind="fact")
    assert [e.id for e in facts] == [fact.id]

    # delete one
    assert await store.delete(pref.id) is True
    remaining_ids = {e.id for e in await store.list()}
    assert pref.id not in remaining_ids

    # delete unknown → False, no throw
    assert await store.delete("mem_doesnotexist") is False

    # clear by kind
    cleared = await store.clear(kind="pattern")
    assert cleared == 1
    assert await store.list(kind="pattern") == []
    assert len(await store.list()) == 1  # just `fact`

    # clear all
    cleared_all = await store.clear()
    assert cleared_all == 1
    assert await store.list() == []


@pytest.mark.asyncio
async def test_rejects_bad_kind_and_empty_title() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    store = MemoryStore(settings.db_path)

    with pytest.raises(ValueError):
        await store.add(kind="notakind", title="x", body="y")
    with pytest.raises(ValueError):
        await store.add(kind="preference", title="   ", body="y")
    with pytest.raises(ValueError):
        await store.list(kind="notakind")
