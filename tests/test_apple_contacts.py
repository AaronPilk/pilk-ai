"""Tests for the macOS Contacts read tool.

Every test stubs ``_run_osascript`` so the suite runs cross-platform
(CI is Linux; production is macOS). The parser round-trip is
exercised end-to-end against synthetic AppleScript output using the
exact field separators the real script emits, so a future change to
those separators can't silently break decoding.
"""

from __future__ import annotations

import pytest

from core.integrations.apple import contacts as contacts_mod
from core.integrations.apple.contacts import (
    _FIELD_SEP,
    _ROW_SEP,
    _SUB_SEP,
    MAX_CONTACT_RESULTS,
    ContactsSearchError,
    _parse_contacts,
    make_contacts_tools,
)
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext


def _row(name: str, emails: list[str], phones: list[str], company: str) -> str:
    return _FIELD_SEP.join(
        [
            name,
            _SUB_SEP.join(emails),
            _SUB_SEP.join(phones),
            company,
        ]
    )


def _output(rows: list[str]) -> str:
    return _ROW_SEP.join(rows)


# ── parser unit tests ────────────────────────────────────────────


def test_parse_empty_output() -> None:
    assert _parse_contacts("") == []


def test_parse_single_row() -> None:
    raw = _row(
        "Jane Doe",
        ["jane@x.com"],
        ["+14155551234"],
        "Acme",
    )
    [c] = _parse_contacts(raw)
    assert c == {
        "name": "Jane Doe",
        "emails": ["jane@x.com"],
        "phones": ["+14155551234"],
        "company": "Acme",
    }


def test_parse_multi_valued_fields() -> None:
    raw = _row(
        "Jane Doe",
        ["jane@x.com", "jane@y.com"],
        ["+14155551234", "+14155551235"],
        "",
    )
    [c] = _parse_contacts(raw)
    assert c["emails"] == ["jane@x.com", "jane@y.com"]
    assert c["phones"] == ["+14155551234", "+14155551235"]
    assert c["company"] == ""


def test_parse_multiple_rows() -> None:
    raw = _output([
        _row("Alice", ["a@x.com"], [], "A Corp"),
        _row("Bob", [], ["+15551234567"], ""),
    ])
    results = _parse_contacts(raw)
    assert len(results) == 2
    assert results[0]["name"] == "Alice"
    assert results[1]["name"] == "Bob"
    assert results[1]["emails"] == []


def test_parse_handles_missing_trailing_fields() -> None:
    """AppleScript sometimes emits rows with fewer than 4 fields if
    the contact has no organization. Parser pads to avoid IndexError."""
    # Only name + emails, no phones / company fields at all.
    raw = "Charlie" + _FIELD_SEP + "c@x.com"
    [c] = _parse_contacts(raw)
    assert c == {
        "name": "Charlie",
        "emails": ["c@x.com"],
        "phones": [],
        "company": "",
    }


# ── tool surface ─────────────────────────────────────────────────


def test_tool_metadata_and_risk() -> None:
    [tool] = make_contacts_tools()
    assert tool.name == "contacts_search"
    assert tool.risk == RiskClass.READ
    assert tool.account_binding is None


# ── tool handler happy + error paths ─────────────────────────────


@pytest.mark.asyncio
async def test_search_happy_path(monkeypatch) -> None:
    payload = _output([
        _row("Jane Doe", ["jane@acme.com"], ["+14155551234"], "Acme"),
        _row("Jane Smith", [], ["+14155559999"], ""),
    ])
    monkeypatch.setattr(
        contacts_mod, "_run_osascript", lambda s, *a: payload,
    )
    [tool] = make_contacts_tools()
    out = await tool.handler({"query": "jane"}, ToolContext())
    assert not out.is_error
    assert len(out.data["results"]) == 2
    assert out.data["results"][0]["name"] == "Jane Doe"
    assert "jane@acme.com" in out.content
    assert "+14155559999" in out.content


@pytest.mark.asyncio
async def test_search_empty_results(monkeypatch) -> None:
    monkeypatch.setattr(
        contacts_mod, "_run_osascript", lambda s, *a: "",
    )
    [tool] = make_contacts_tools()
    out = await tool.handler({"query": "ghost"}, ToolContext())
    assert not out.is_error
    assert out.data["results"] == []
    assert "No contacts" in out.content


@pytest.mark.asyncio
async def test_search_missing_query_is_error() -> None:
    [tool] = make_contacts_tools()
    out = await tool.handler({}, ToolContext())
    assert out.is_error
    assert "query" in out.content.lower()


@pytest.mark.asyncio
async def test_search_permission_error_is_surfaced(monkeypatch) -> None:
    def refuse(script: str, *args: str) -> str:
        raise ContactsSearchError(
            "macOS refused access to Contacts. Open System Settings …"
        )

    monkeypatch.setattr(contacts_mod, "_run_osascript", refuse)
    [tool] = make_contacts_tools()
    out = await tool.handler({"query": "jane"}, ToolContext())
    assert out.is_error
    assert "Contacts" in out.content or "access" in out.content


@pytest.mark.asyncio
async def test_search_passes_max_results_to_osascript(monkeypatch) -> None:
    """Lock in that the clamped max_results lands in the AppleScript
    argv (second arg) — otherwise the cap is silently bypassable."""
    seen: list[tuple[str, tuple[str, ...]]] = []

    def capture(script: str, *args: str) -> str:
        seen.append((script, args))
        return ""

    monkeypatch.setattr(contacts_mod, "_run_osascript", capture)
    [tool] = make_contacts_tools()
    await tool.handler(
        {"query": "jane", "max_results": 5}, ToolContext(),
    )
    _script, args = seen[0]
    assert args == ("jane", "5")


@pytest.mark.asyncio
async def test_search_clamps_oversize_max_results(monkeypatch) -> None:
    seen: list[tuple[str, ...]] = []

    def capture(script: str, *args: str) -> str:
        seen.append(args)
        return ""

    monkeypatch.setattr(contacts_mod, "_run_osascript", capture)
    [tool] = make_contacts_tools()
    await tool.handler(
        {"query": "jane", "max_results": 9999}, ToolContext(),
    )
    assert seen[0][1] == str(MAX_CONTACT_RESULTS)
