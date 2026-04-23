"""ChatGPT vault index — build, load, topic classification, query."""
from __future__ import annotations

from datetime import UTC
from pathlib import Path

from core.brain.chatgpt_index import (
    CHATGPT_DIR,
    INDEX_FILE,
    IndexEntry,
    build_index,
    classify_topic,
    load_index,
    query_chatgpt_vault,
)

# ── fixtures ─────────────────────────────────────────────────────────


def _write_note(
    vault_root: Path, rel: str, body: str,
) -> Path:
    full = vault_root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    return full


def _seed_vault(root: Path) -> None:
    _write_note(
        root, f"{CHATGPT_DIR}/2024-01-05-gold-scalping.md",
        "# Gold scalping strategy\n\nDiscussing XAUUSD entry on the 5m chart after the London open.\n",
    )
    _write_note(
        root, f"{CHATGPT_DIR}/2024-02-10-nv-brand-rename.md",
        "# NV brand rename\n\nReplacing the logo, packaging, and label on the new palette.\n",
    )
    _write_note(
        root, f"{CHATGPT_DIR}/2024-03-15-saas-pricing.md",
        "# SaaS pricing for the CRM\n\nOffer + funnel + MRR math for the new tier.\n",
    )
    _write_note(
        root, f"{CHATGPT_DIR}/2024-04-20-workout-plan.md",
        "# Health check\n\nRelationships, diet, workout, mindset — a full personal reset.\n",
    )
    _write_note(
        root, f"{CHATGPT_DIR}/2024-05-01-fastapi-deploy.md",
        "# FastAPI deploy notes\n\nPython, docker, api, refactor.\n",
    )
    _write_note(
        root, f"{CHATGPT_DIR}/2024-06-01-random-chatter.md",
        "# Random chatter\n\nJust a misc conversation with nothing in particular.\n",
    )


# ── classify_topic ──────────────────────────────────────────────────


def test_classify_topic_buckets() -> None:
    assert classify_topic("XAUUSD gold chart candle") == "trading"
    assert classify_topic("brand, logo, and packaging for NV") == "brand"
    assert classify_topic("revenue, offer, SaaS, startup pitch") == "business"
    assert classify_topic("mindset and relationships journal") == "personal"
    assert classify_topic("python api deploy agent") == "tech"
    assert classify_topic("nothing noteworthy in here") == "general"
    assert classify_topic("") == "general"


def test_classify_topic_specific_wins_over_broad() -> None:
    """'trade' beats 'code' when both keywords appear."""
    assert classify_topic(
        "trade setup for gold; also some python code notes below",
    ) == "trading"


# ── build_index / load_index ────────────────────────────────────────


def test_build_index_classifies_keywords_past_preview_window(
    tmp_path: Path,
) -> None:
    """Previously the classifier only saw the first 300 chars so a
    conversation that opened with small talk and only got to the topic
    keywords later dropped to ``general``. Regression test: a body
    where the topic keywords sit well past the preview window must
    still bucket correctly."""
    chat_dir = tmp_path / "ingested" / "chatgpt"
    chat_dir.mkdir(parents=True)

    # 1200 chars of pleasantries before any topical keyword appears.
    # This is well past the old 300-char classifier window — only
    # picks up ``business`` if the classifier reads deep into the body.
    filler = (
        "Hey, hope your day is going well. Just wanted to catch up and "
        "talk about something I've been mulling over for a while now. "
    )
    while len(filler) < 1200:
        filler += "It's been a weird week, lots of small things adding up. "

    body = (
        "# Thinking out loud\n\n"
        + filler
        + "\n\nOK here's the thing — I want to put together a pitch for a "
        "SaaS with recurring revenue, really nail the GTM funnel and pricing.\n"
    )
    (chat_dir / "2026-04-22-thinking.md").write_text(body, encoding="utf-8")

    n = build_index(tmp_path)
    assert n == 1

    entries = load_index(tmp_path)
    assert entries[0].topic == "business"


def test_build_index_writes_jsonl(tmp_path: Path) -> None:
    _seed_vault(tmp_path)
    n = build_index(tmp_path)
    assert n == 6

    idx = tmp_path / INDEX_FILE
    assert idx.exists()
    lines = idx.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 6

    entries = load_index(tmp_path)
    by_topic = {}
    for e in entries:
        by_topic.setdefault(e.topic, []).append(e.path)
    assert {"trading", "brand", "business", "personal", "tech", "general"} <= by_topic.keys()
    # Title comes from the # heading, not the filename stem.
    titles = {e.title for e in entries}
    assert "Gold scalping strategy" in titles
    assert "SaaS pricing for the CRM" in titles


def test_build_index_overwrites_previous(tmp_path: Path) -> None:
    """Rebuilding after a new note lands picks it up; removed notes
    drop out."""
    _seed_vault(tmp_path)
    build_index(tmp_path)
    assert len(load_index(tmp_path)) == 6

    _write_note(
        tmp_path, f"{CHATGPT_DIR}/2024-07-02-new-trade-idea.md",
        "# Trade idea\n\nGold scalp at 2350.\n",
    )
    assert build_index(tmp_path) == 7
    assert len(load_index(tmp_path)) == 7


def test_build_index_skips_index_sidecar(tmp_path: Path) -> None:
    """The ``_index.jsonl`` file itself is not treated as a note."""
    _seed_vault(tmp_path)
    build_index(tmp_path)
    # Underscore-prefixed files never get indexed.
    rebuilt = build_index(tmp_path)
    assert rebuilt == 6


def test_build_index_missing_chatgpt_dir_writes_empty(tmp_path: Path) -> None:
    """No ingested/chatgpt dir yet → empty index, not an exception."""
    n = build_index(tmp_path)
    assert n == 0
    assert (tmp_path / INDEX_FILE).exists()
    assert load_index(tmp_path) == []


# ── query ───────────────────────────────────────────────────────────


def test_query_returns_top_matches(tmp_path: Path) -> None:
    _seed_vault(tmp_path)
    build_index(tmp_path)
    hits = query_chatgpt_vault(tmp_path, "gold scalping", top_k=3)
    assert hits, "expected at least one hit for 'gold scalping'"
    top = hits[0].entry
    assert "gold-scalping" in top.path or top.topic == "trading"


def test_query_title_weights_more_than_preview(tmp_path: Path) -> None:
    _write_note(
        tmp_path, f"{CHATGPT_DIR}/a.md",
        "# Unrelated note\n\nLots of apple apple apple mentions in the body.\n",
    )
    _write_note(
        tmp_path, f"{CHATGPT_DIR}/b.md",
        "# Apple pricing strategy\n\nJust a single apple mention.\n",
    )
    build_index(tmp_path)
    hits = query_chatgpt_vault(tmp_path, "apple", top_k=2)
    assert len(hits) == 2
    # Title hit (3x weight) beats 3 preview hits (1x each) when the
    # other note has 1 title + 1 preview hit.
    assert hits[0].entry.path.endswith("b.md")


def test_query_topic_filter(tmp_path: Path) -> None:
    _seed_vault(tmp_path)
    build_index(tmp_path)
    hits = query_chatgpt_vault(
        tmp_path, "gold", top_k=5, topic="trading",
    )
    assert hits and all(h.entry.topic == "trading" for h in hits)


def test_query_empty_string_returns_empty(tmp_path: Path) -> None:
    _seed_vault(tmp_path)
    build_index(tmp_path)
    assert query_chatgpt_vault(tmp_path, "") == []
    assert query_chatgpt_vault(tmp_path, "   ") == []


def test_index_entry_roundtrip() -> None:
    e = IndexEntry(
        path="ingested/chatgpt/x.md",
        title="Title",
        preview="preview body",
        topic="tech",
        mtime="2024-01-01T00:00:00+00:00",
        size=42,
    )
    line = e.to_json_line()
    back = IndexEntry.from_json_line(line)
    assert back == e


def test_index_entry_from_bad_line_is_none() -> None:
    assert IndexEntry.from_json_line("not json") is None
    assert IndexEntry.from_json_line("[]") is None  # not a dict


# ── scheduler math ──────────────────────────────────────────────────


def test_seconds_until_next_future_same_day() -> None:
    """Target later today → seconds until target later today."""
    from datetime import datetime, time

    from core.brain.chatgpt_index import _seconds_until_next

    now = datetime(2026, 4, 23, 1, 0, 0, tzinfo=UTC)
    # Target 03:00 same day → 2 hours.
    assert _seconds_until_next(now, time(3, 0)) == 2 * 3600


def test_seconds_until_next_wraps_to_tomorrow() -> None:
    """Target already passed today → schedule it for tomorrow."""
    from datetime import datetime, time

    from core.brain.chatgpt_index import _seconds_until_next

    now = datetime(2026, 4, 23, 5, 0, 0, tzinfo=UTC)
    # Target 03:00 has passed → 22 hours until tomorrow's 03:00.
    assert _seconds_until_next(now, time(3, 0)) == 22 * 3600


def test_seconds_until_next_exactly_on_time_wraps() -> None:
    """At exactly the target second we still wait a full day — avoids
    firing twice if the loop tick lands right at 03:00:00.000."""
    from datetime import datetime, time

    from core.brain.chatgpt_index import _seconds_until_next

    now = datetime(2026, 4, 23, 3, 0, 0, tzinfo=UTC)
    assert _seconds_until_next(now, time(3, 0)) == 24 * 3600
