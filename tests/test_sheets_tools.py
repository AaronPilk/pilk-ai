"""Tests for the Google Sheets tool factory.

We mock the ``credentials_from_blob`` loader and the downstream Google
client chain (same pattern as test_slides_create.py) so no HTTP traffic
or real OAuth tokens are required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.identity import AccountsStore
from core.identity.accounts import OAuthTokens
from core.integrations.google.sheets import (
    MAX_APPEND_ROWS,
    make_sheets_tools,
)
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext


@pytest.fixture
def accounts(tmp_path: Path) -> AccountsStore:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    return store


def _link_user_google(store: AccountsStore) -> None:
    store.upsert(
        provider="google",
        role="user",
        label="test",
        email="x@test.com",
        username=None,
        scopes=["sheets.edit", "drive.file"],
        tokens=OAuthTokens(
            access_token="at",
            refresh_token="rt",
            client_id="cid",
            client_secret="cs",
            scopes=["sheets.edit", "drive.file"],
        ),
        make_default=True,
    )


# ── registry shape ──────────────────────────────────────────────


def test_factory_returns_two_tools(accounts: AccountsStore) -> None:
    tools = make_sheets_tools(accounts)
    assert [t.name for t in tools] == [
        "sheets_create",
        "sheets_append_rows",
    ]


def test_sheets_tools_are_net_write(accounts: AccountsStore) -> None:
    """Both tools write to the user's Drive — NET_WRITE routes through
    the approval queue for an operator with gated comms, and keeps the
    risk model consistent with slides_create."""
    for t in make_sheets_tools(accounts):
        assert t.risk == RiskClass.NET_WRITE, t.name


# ── not-linked path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_errors_when_no_account(
    accounts: AccountsStore,
) -> None:
    create, _append = make_sheets_tools(accounts)
    out = await create.handler({"title": "t"}, ToolContext())
    assert out.is_error
    assert "Google account" in out.content


@pytest.mark.asyncio
async def test_append_errors_when_no_account(
    accounts: AccountsStore,
) -> None:
    _create, append = make_sheets_tools(accounts)
    out = await append.handler(
        {"spreadsheet_id": "abc", "rows": [["a"]]}, ToolContext(),
    )
    assert out.is_error
    assert "Google account" in out.content


# ── argument validation ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_rejects_empty_title(accounts: AccountsStore) -> None:
    _link_user_google(accounts)
    create, _append = make_sheets_tools(accounts)
    out = await create.handler({"title": "   "}, ToolContext())
    assert out.is_error
    assert "non-empty title" in out.content


@pytest.mark.asyncio
async def test_create_rejects_non_string_header(
    accounts: AccountsStore,
) -> None:
    _link_user_google(accounts)
    create, _append = make_sheets_tools(accounts)
    out = await create.handler(
        {"title": "t", "header": ["name", 42]}, ToolContext(),
    )
    assert out.is_error
    assert "header must contain only strings" in out.content


@pytest.mark.asyncio
async def test_append_requires_spreadsheet_id(
    accounts: AccountsStore,
) -> None:
    _link_user_google(accounts)
    _create, append = make_sheets_tools(accounts)
    out = await append.handler(
        {"spreadsheet_id": "", "rows": [["a"]]}, ToolContext(),
    )
    assert out.is_error
    assert "spreadsheet_id" in out.content


@pytest.mark.asyncio
async def test_append_requires_rows(accounts: AccountsStore) -> None:
    _link_user_google(accounts)
    _create, append = make_sheets_tools(accounts)
    out = await append.handler(
        {"spreadsheet_id": "abc", "rows": []}, ToolContext(),
    )
    assert out.is_error
    assert "non-empty 'rows' list" in out.content


@pytest.mark.asyncio
async def test_append_rejects_non_list_row(
    accounts: AccountsStore,
) -> None:
    _link_user_google(accounts)
    _create, append = make_sheets_tools(accounts)
    out = await append.handler(
        {"spreadsheet_id": "abc", "rows": [["ok"], "bad"]},
        ToolContext(),
    )
    assert out.is_error
    assert "row 1 is not a list" in out.content


@pytest.mark.asyncio
async def test_append_rejects_overflow_batch(
    accounts: AccountsStore,
) -> None:
    _link_user_google(accounts)
    _create, append = make_sheets_tools(accounts)
    out = await append.handler(
        {
            "spreadsheet_id": "abc",
            "rows": [["x"] for _ in range(MAX_APPEND_ROWS + 1)],
        },
        ToolContext(),
    )
    assert out.is_error
    assert "Split into batches" in out.content


# ── happy path (mocked Google client) ───────────────────────────


class _FakeExecute:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def execute(self) -> dict[str, Any]:
        return self._payload


class _FakeValues:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def update(self, **kwargs):
        self._captured["values_update"] = kwargs
        return _FakeExecute({})

    def append(self, **kwargs):
        self._captured["values_append"] = kwargs
        return _FakeExecute({
            "updates": {
                "updatedRange": "Sheet1!A2:C4",
                "updatedRows": 3,
            }
        })


class _FakeSpreadsheets:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def create(self, body=None):
        self._captured["create_body"] = body
        return _FakeExecute({"spreadsheetId": "sheet-xyz"})

    def values(self):
        return _FakeValues(self._captured)


class _FakeService:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def spreadsheets(self):
        return _FakeSpreadsheets(self._captured)


class _FakeCreds:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured
        self.email = "x@test.com"

    def build(self, api, version):
        assert api == "sheets"
        assert version == "v4"
        return _FakeService(self._captured)


def _patch_creds_loader(monkeypatch, captured: dict[str, Any]) -> None:
    from core.integrations.google import sheets as sheets_mod

    def fake_credentials_from_blob(blob):
        return _FakeCreds(captured)

    monkeypatch.setattr(
        sheets_mod, "credentials_from_blob", fake_credentials_from_blob
    )


@pytest.mark.asyncio
async def test_create_writes_header_when_provided(
    accounts: AccountsStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _link_user_google(accounts)
    captured: dict[str, Any] = {}
    _patch_creds_loader(monkeypatch, captured)

    create, _append = make_sheets_tools(accounts)
    out = await create.handler(
        {"title": "Leads — CPAs", "header": ["name", "email"]},
        ToolContext(),
    )
    assert not out.is_error, out.content
    assert out.data["spreadsheet_id"] == "sheet-xyz"
    assert "docs.google.com/spreadsheets" in out.data["url"]
    assert captured["create_body"] == {"properties": {"title": "Leads — CPAs"}}
    # Header written in one targeted values().update() call to A1.
    assert captured["values_update"]["range"] == "Sheet1!A1"
    assert captured["values_update"]["body"] == {
        "values": [["name", "email"]],
    }


@pytest.mark.asyncio
async def test_create_skips_header_call_when_none(
    accounts: AccountsStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _link_user_google(accounts)
    captured: dict[str, Any] = {}
    _patch_creds_loader(monkeypatch, captured)

    create, _append = make_sheets_tools(accounts)
    out = await create.handler(
        {"title": "Blank sheet"}, ToolContext(),
    )
    assert not out.is_error
    assert "values_update" not in captured  # no header → no second call


@pytest.mark.asyncio
async def test_append_batches_all_rows_in_one_call(
    accounts: AccountsStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _link_user_google(accounts)
    captured: dict[str, Any] = {}
    _patch_creds_loader(monkeypatch, captured)

    _create, append = make_sheets_tools(accounts)
    rows = [["acme", "a@acme.com"], ["globex", "g@globex.com"]]
    out = await append.handler(
        {
            "spreadsheet_id": "sheet-xyz",
            "tab_name": "Sheet1",
            "rows": rows,
        },
        ToolContext(),
    )
    assert not out.is_error, out.content
    assert out.data["rows_appended"] == 2
    assert captured["values_append"]["spreadsheetId"] == "sheet-xyz"
    # Single append call with ALL rows in one body — the agent must
    # not loop one row per call.
    assert captured["values_append"]["body"] == {"values": rows}
    assert captured["values_append"]["range"] == "Sheet1!A1"
    assert captured["values_append"]["insertDataOption"] == "INSERT_ROWS"


@pytest.mark.asyncio
async def test_append_defaults_tab_to_sheet1(
    accounts: AccountsStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _link_user_google(accounts)
    captured: dict[str, Any] = {}
    _patch_creds_loader(monkeypatch, captured)

    _create, append = make_sheets_tools(accounts)
    out = await append.handler(
        {"spreadsheet_id": "abc", "rows": [["x"]]},
        ToolContext(),
    )
    assert not out.is_error
    # Default tab name on append matches the default tab new sheets
    # come with so "create then append" works without the caller
    # having to pass tab_name.
    assert captured["values_append"]["range"] == "Sheet1!A1"
