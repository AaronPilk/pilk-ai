"""Drive + Calendar tool factories — structural checks.

These don't hit Google's real API. We verify that the factories emit
the expected tool names bound to (google, user) and refuse cleanly
when no account is connected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.identity import AccountsStore
from core.integrations.google import make_calendar_tools, make_drive_tools
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext


def _empty_store(tmp_path: Path) -> AccountsStore:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    return store


def test_drive_tool_names_and_bindings(tmp_path: Path) -> None:
    tools = make_drive_tools(_empty_store(tmp_path))
    names = [t.name for t in tools]
    assert names == ["drive_search_my_files", "drive_read_my_file"]
    for t in tools:
        assert t.account_binding is not None
        assert t.account_binding.provider == "google"
        assert t.account_binding.role == "user"
        assert t.risk == RiskClass.NET_READ


def test_calendar_tool_names_risk_and_bindings(tmp_path: Path) -> None:
    tools = make_calendar_tools(_empty_store(tmp_path))
    names = [t.name for t in tools]
    assert names == ["calendar_read_my_today", "calendar_create_my_event"]
    read, create = tools
    assert read.risk == RiskClass.NET_READ
    assert create.risk == RiskClass.NET_WRITE
    for t in tools:
        assert t.account_binding is not None
        assert t.account_binding.provider == "google"
        assert t.account_binding.role == "user"


@pytest.mark.asyncio
async def test_drive_tools_refuse_when_not_linked(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    [search, read] = make_drive_tools(store)
    search_result = await search.handler({"query": "contract"}, ToolContext())
    assert search_result.is_error is True
    assert "Expand access" in search_result.content
    read_result = await read.handler({"file_id": "abc"}, ToolContext())
    assert read_result.is_error is True


@pytest.mark.asyncio
async def test_calendar_tools_refuse_when_not_linked(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    [read, create] = make_calendar_tools(store)
    read_result = await read.handler({}, ToolContext())
    assert read_result.is_error is True
    create_result = await create.handler(
        {
            "summary": "x",
            "start": "2026-04-20T14:00:00-07:00",
            "end": "2026-04-20T14:30:00-07:00",
        },
        ToolContext(),
    )
    assert create_result.is_error is True
    assert "Expand access" in create_result.content
