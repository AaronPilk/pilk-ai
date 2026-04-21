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
    assert names == [
        "calendar_read_my_today",
        "calendar_read_my_range",
        "calendar_create_my_event",
    ]
    read, read_range, create = tools
    assert read.risk == RiskClass.NET_READ
    assert read_range.risk == RiskClass.NET_READ
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
    [read, read_range, create] = make_calendar_tools(store)
    read_result = await read.handler({}, ToolContext())
    assert read_result.is_error is True
    read_range_result = await read_range.handler(
        {"start": "2026-04-20", "end": "2026-04-27"}, ToolContext(),
    )
    assert read_range_result.is_error is True
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


@pytest.mark.asyncio
async def test_calendar_range_validation_fires_before_creds_check(
    tmp_path: Path,
) -> None:
    """The range tool validates start/end/span BEFORE consulting
    the accounts store, so a malformed call surfaces the real error
    instead of a generic 'Expand access'. Locks in the input-first
    ordering so a future refactor doesn't regress it."""
    store = _empty_store(tmp_path)
    [_read, read_range, _create] = make_calendar_tools(store)
    # Missing fields.
    out = await read_range.handler({"start": "2026-04-20"}, ToolContext())
    assert out.is_error is True
    assert "'start' and 'end'" in out.content

    # Malformed date (not ISO).
    out = await read_range.handler(
        {"start": "next monday", "end": "2026-04-27"}, ToolContext(),
    )
    assert out.is_error is True
    assert "invalid date" in out.content.lower()

    # Inverted range.
    out = await read_range.handler(
        {"start": "2026-04-27", "end": "2026-04-20"}, ToolContext(),
    )
    assert out.is_error is True
    assert ">=" in out.content

    # Too wide (>92 days).
    out = await read_range.handler(
        {"start": "2026-01-01", "end": "2026-12-31"}, ToolContext(),
    )
    assert out.is_error is True
    assert "too wide" in out.content.lower()
