"""Identity bootstrap — the seed set must land, be stable across
reruns, and overwrite stale rows that share its ids."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from core.db.migrations import ensure_schema
from core.identity.bootstrap import _SEEDS, seed_identity_memory


def _all_identity_rows(db_path: Path) -> dict[str, tuple[str, str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, title, body, source FROM memory_entries "
            "WHERE id LIKE 'identity-%'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def test_seed_writes_full_set(tmp_path: Path) -> None:
    db = tmp_path / "pilk.db"
    ensure_schema(db)

    written = seed_identity_memory(db)
    assert written == len(_SEEDS)

    rows = _all_identity_rows(db)
    expected_ids = {s.id for s in _SEEDS}
    assert set(rows.keys()) == expected_ids
    # All seed rows must be tagged as system-authored so operator-
    # curated memories stay distinguishable.
    for _id, (_title, _body, source) in rows.items():
        assert source == "system"


def test_seed_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "pilk.db"
    ensure_schema(db)

    seed_identity_memory(db)
    seed_identity_memory(db)
    seed_identity_memory(db)

    rows = _all_identity_rows(db)
    assert len(rows) == len(_SEEDS)


def test_seed_overwrites_stale_row(tmp_path: Path) -> None:
    """If an older PILK version seeded a different body for the same
    id, the next boot must replace it with the canonical text."""
    db = tmp_path / "pilk.db"
    ensure_schema(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO memory_entries("
            "id, kind, title, body, source, plan_id, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "identity-acronym",
                "fact",
                "stale title",
                "stale body that should be overwritten",
                "system",
                None,
                "2020-01-01T00:00:00+00:00",
                "2020-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    seed_identity_memory(db)

    rows = _all_identity_rows(db)
    title, body, _src = rows["identity-acronym"]
    assert title.startswith("PILK")
    assert "Personal Intelligence Large-Language Kit" in body


def test_seed_contains_north_star(tmp_path: Path) -> None:
    """Sanity check on the identity content itself — if someone
    rewrites the seeds, the north star must stay on the list. Losing
    it would quietly change how every agent makes decisions."""
    db = tmp_path / "pilk.db"
    ensure_schema(db)
    seed_identity_memory(db)

    rows = _all_identity_rows(db)
    assert "identity-north-star" in rows
    _title, body, _ = rows["identity-north-star"]
    assert "financial freedom" in body.lower()
