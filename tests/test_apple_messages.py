"""Apple Messages local reader — availability + read against a fake chat.db.

Builds a minimal in-repo SQLite file that mirrors the subset of the
Messages.app schema PILK reads, then points `PILK_APPLE_MESSAGES_DB`
at it for the duration of the test. Keeps the tests hermetic and
cross-platform (runs on Linux CI even though production is macOS).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.integrations.apple import (
    check_messages_status,
    make_messages_tools,
    read_thread,
    recent_threads,
)
from core.integrations.apple.messages import MAC_EPOCH, search_messages
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext


def _build_fake_chat_db(path: Path) -> None:
    """Create a DB with the minimum schema PILK's reader expects."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE handle (
              ROWID INTEGER PRIMARY KEY,
              id TEXT
            );
            CREATE TABLE chat (
              ROWID INTEGER PRIMARY KEY,
              display_name TEXT,
              chat_identifier TEXT,
              style INTEGER
            );
            CREATE TABLE chat_handle_join (
              chat_id INTEGER,
              handle_id INTEGER
            );
            CREATE TABLE chat_message_join (
              chat_id INTEGER,
              message_id INTEGER
            );
            CREATE TABLE message (
              ROWID INTEGER PRIMARY KEY,
              text TEXT,
              date INTEGER,
              is_from_me INTEGER,
              handle_id INTEGER,
              cache_has_attachments INTEGER DEFAULT 0
            );
            """
        )
        conn.execute(
            "INSERT INTO handle(ROWID, id) VALUES (?, ?)",
            (1, "+14155551234"),
        )
        conn.execute(
            "INSERT INTO chat(ROWID, display_name, chat_identifier, style) "
            "VALUES (?, ?, ?, ?)",
            (10, "Jane", "+14155551234", 45),  # 45 = direct
        )
        conn.execute(
            "INSERT INTO chat(ROWID, display_name, chat_identifier, style) "
            "VALUES (?, ?, ?, ?)",
            (11, "Fam", "chat000000000", 43),  # 43 = group
        )
        now = datetime.now(UTC)
        mac_now = int((now - MAC_EPOCH).total_seconds() * 1e9)

        def _insert(message_id, chat_id, text, is_from_me, handle_id, ago_s=0):
            ts = mac_now - int(ago_s * 1e9)
            conn.execute(
                "INSERT INTO message(ROWID, text, date, is_from_me, handle_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (message_id, text, ts, is_from_me, handle_id),
            )
            conn.execute(
                "INSERT INTO chat_message_join(chat_id, message_id) VALUES (?, ?)",
                (chat_id, message_id),
            )

        # Jane thread — two messages; Jane's latest is the most recent overall.
        _insert(100, 10, "hey whats up", 0, 1, ago_s=600)
        _insert(101, 10, "running late, 10 min?", 0, 1, ago_s=60)
        # Family group thread — older.
        _insert(200, 11, "grocery list incoming", 1, None, ago_s=3600)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def fake_db(tmp_path: Path, monkeypatch):
    db = tmp_path / "chat.db"
    _build_fake_chat_db(db)
    monkeypatch.setenv("PILK_APPLE_MESSAGES_DB", str(db))
    return db


def test_status_unavailable_when_db_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PILK_APPLE_MESSAGES_DB", str(tmp_path / "nope.db"))
    status = check_messages_status()
    assert status.available is False
    assert "not found" in (status.reason or "")


def test_status_available_with_fixture(fake_db: Path) -> None:
    status = check_messages_status()
    assert status.available is True
    assert status.db_path == str(fake_db)


def test_recent_threads_returns_newest_first(fake_db: Path) -> None:
    threads = recent_threads(limit=5)
    assert [t["title"] for t in threads] == ["Jane", "Fam"]
    jane = threads[0]
    assert jane["last_snippet"] == "running late, 10 min?"
    assert jane["last_from_me"] is False
    assert jane["is_group"] is False
    fam = threads[1]
    assert fam["is_group"] is True
    assert fam["last_from_me"] is True


def test_read_thread_returns_chronological_messages(fake_db: Path) -> None:
    thread = read_thread(chat_id=10, limit=10)
    assert thread["title"] == "Jane"
    # Chronological (oldest → newest) after the read helper reverses.
    assert [m["text"] for m in thread["messages"]] == [
        "hey whats up",
        "running late, 10 min?",
    ]


def test_search_matches_substring(fake_db: Path) -> None:
    results = search_messages("grocery")
    assert len(results) == 1
    assert results[0]["title"] == "Fam"


def test_tools_metadata_and_risk() -> None:
    [search, read] = make_messages_tools()
    assert search.name == "messages_search_mine"
    assert read.name == "messages_read_thread"
    assert search.risk == RiskClass.READ
    assert read.risk == RiskClass.READ
    # These tools are local reads — no account_binding.
    assert search.account_binding is None
    assert read.account_binding is None


@pytest.mark.asyncio
async def test_tool_handler_returns_results(fake_db: Path) -> None:
    [search, read] = make_messages_tools()
    result = await search.handler({"query": "late"}, ToolContext())
    assert result.is_error is False
    assert "running late" in result.content

    thread_result = await read.handler({"chat_id": 10}, ToolContext())
    assert thread_result.is_error is False
    assert "running late" in thread_result.content


@pytest.mark.asyncio
async def test_tool_handler_errors_when_db_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PILK_APPLE_MESSAGES_DB", str(tmp_path / "nope.db"))
    [search, _read] = make_messages_tools()
    out = await search.handler({"query": "x"}, ToolContext())
    # Missing DB → empty results, not a crash.
    assert out.is_error is False
    assert "No messages match" in out.content
