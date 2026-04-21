"""Tests for GHL contacts + meta tools — PR #75c.

The client methods are already covered by test_ghl_client.py (from
PR #75a). This module tests the 11 **tool handlers** that wrap them:

- 8 contacts: create / get / search / update / delete / add_tag /
  remove_tag / add_note
- 3 meta:    location_list / user_list / custom_field_list

Each tool gets coverage for:
- validation (required fields, at-least-one-of constraints)
- happy path (sends right payload, returns right shape)
- error rewrite (401/403/429 hint, raw preserved)

Factory-level tests lock in the shape the lifespan registers
(names, risk classes).
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable

import httpx
import pytest

from core.config import get_settings
from core.integrations.ghl import make_ghl_contact_tools
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext

# ── httpx mock plumbing (mirrors test_ghl_pipelines.py) ──────────


def _install_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _restore_httpx_init():
    original = httpx.AsyncClient.__init__
    yield
    httpx.AsyncClient.__init__ = original  # type: ignore[method-assign]


@pytest.fixture
def ghl_key(monkeypatch) -> str:
    monkeypatch.setenv("GHL_API_KEY", "pit_test")
    monkeypatch.setenv("GHL_DEFAULT_LOCATION_ID", "loc_default")
    get_settings.cache_clear()
    yield "pit_test"
    get_settings.cache_clear()


def _get(name: str):
    for t in make_ghl_contact_tools():
        if t.name == name:
            return t
    raise AssertionError(f"no tool named {name}")


# ── factory shape ────────────────────────────────────────────────


def test_factory_emits_all_eleven() -> None:
    tools = make_ghl_contact_tools()
    names = sorted(t.name for t in tools)
    assert names == sorted([
        "ghl_contact_create",
        "ghl_contact_get",
        "ghl_contact_search",
        "ghl_contact_update",
        "ghl_contact_delete",
        "ghl_contact_add_tag",
        "ghl_contact_remove_tag",
        "ghl_contact_add_note",
        "ghl_location_list",
        "ghl_user_list",
        "ghl_custom_field_list",
    ])


def test_risk_classes() -> None:
    """Reads are NET_READ, every mutation is NET_WRITE. Locks in
    the approval-queue routing so a silent drop to a lower risk
    doesn't skip the gate."""
    read_names = {
        "ghl_contact_get",
        "ghl_contact_search",
        "ghl_location_list",
        "ghl_user_list",
        "ghl_custom_field_list",
    }
    for tool in make_ghl_contact_tools():
        if tool.name in read_names:
            assert tool.risk == RiskClass.NET_READ, tool.name
        else:
            assert tool.risk == RiskClass.NET_WRITE, tool.name


# ── not-configured + missing-location guards ─────────────────────


@pytest.mark.asyncio
async def test_not_configured_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("GHL_API_KEY", raising=False)
    monkeypatch.delenv("PILK_GHL_API_KEY", raising=False)
    monkeypatch.setenv("GHL_DEFAULT_LOCATION_ID", "loc_default")
    get_settings.cache_clear()
    try:
        out = await _get("ghl_contact_get").handler(
            {"contact_id": "c-1"}, ToolContext(),
        )
        assert out.is_error
        assert "not configured" in out.content.lower()
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_missing_location_on_location_scoped_tool(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GHL_API_KEY", "pit_test")
    monkeypatch.delenv("GHL_DEFAULT_LOCATION_ID", raising=False)
    monkeypatch.delenv("PILK_GHL_DEFAULT_LOCATION_ID", raising=False)
    get_settings.cache_clear()
    try:
        out = await _get("ghl_contact_search").handler(
            {"query": "x"}, ToolContext(),
        )
        assert out.is_error
        assert "location_id" in out.content.lower()
    finally:
        get_settings.cache_clear()


# ── ghl_contact_create ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_requires_email_or_phone(ghl_key: str) -> None:
    out = await _get("ghl_contact_create").handler(
        {"first_name": "Jane"}, ToolContext(),
    )
    assert out.is_error
    assert "email" in out.content.lower() and "phone" in out.content.lower()


@pytest.mark.asyncio
async def test_create_accepts_phone_only(ghl_key: str) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"contact": {"id": "c-new"}})

    _install_transport(handler)
    out = await _get("ghl_contact_create").handler(
        {"phone": "+14155551234"}, ToolContext(),
    )
    assert not out.is_error
    assert captured["body"]["phone"] == "+14155551234"
    assert captured["body"]["locationId"] == "loc_default"


@pytest.mark.asyncio
async def test_create_translates_snake_to_camel(ghl_key: str) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"contact": {"id": "c-new"}})

    _install_transport(handler)
    await _get("ghl_contact_create").handler(
        {
            "email": "jane@example.com",
            "first_name": "Jane",
            "last_name": "Doe",
            "company_name": "Acme",
            "tags": ["lead", "warm"],
        },
        ToolContext(),
    )
    body = captured["body"]
    assert body["email"] == "jane@example.com"
    assert body["firstName"] == "Jane"
    assert body["lastName"] == "Doe"
    assert body["companyName"] == "Acme"
    assert body["tags"] == ["lead", "warm"]


# ── ghl_contact_get ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_requires_contact_id(ghl_key: str) -> None:
    out = await _get("ghl_contact_get").handler({}, ToolContext())
    assert out.is_error
    assert "contact_id" in out.content.lower()


@pytest.mark.asyncio
async def test_get_happy_path(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "contact": {
                    "id": "c-1",
                    "firstName": "Jane",
                    "lastName": "Doe",
                    "email": "jane@example.com",
                    "phone": "+14155551234",
                    "tags": ["lead"],
                },
            },
        )

    _install_transport(handler)
    out = await _get("ghl_contact_get").handler(
        {"contact_id": "c-1"}, ToolContext(),
    )
    assert not out.is_error
    assert "Jane Doe" in out.content
    assert "jane@example.com" in out.content
    assert "lead" in out.content


# ── ghl_contact_search ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_requires_at_least_one_filter(ghl_key: str) -> None:
    out = await _get("ghl_contact_search").handler({}, ToolContext())
    assert out.is_error
    assert "email" in out.content.lower()


@pytest.mark.asyncio
async def test_search_clamps_oversize_limit(ghl_key: str) -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"contacts": []})

    _install_transport(handler)
    await _get("ghl_contact_search").handler(
        {"query": "acme", "limit": 9999}, ToolContext(),
    )
    assert captured[0].url.params.get("limit") == "100"


@pytest.mark.asyncio
async def test_search_happy_path(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "contacts": [
                    {
                        "id": "c-1",
                        "firstName": "Jane",
                        "lastName": "Doe",
                        "email": "jane@acme.com",
                    },
                ],
                "total": 1,
            },
        )

    _install_transport(handler)
    out = await _get("ghl_contact_search").handler(
        {"email": "jane@acme.com"}, ToolContext(),
    )
    assert not out.is_error
    assert out.data["total"] == 1
    assert "Jane Doe" in out.content
    assert "jane@acme.com" in out.content


# ── ghl_contact_update ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_requires_contact_id(ghl_key: str) -> None:
    out = await _get("ghl_contact_update").handler(
        {"first_name": "Janet"}, ToolContext(),
    )
    assert out.is_error
    assert "contact_id" in out.content.lower()


@pytest.mark.asyncio
async def test_update_requires_at_least_one_field(ghl_key: str) -> None:
    out = await _get("ghl_contact_update").handler(
        {"contact_id": "c-1"}, ToolContext(),
    )
    assert out.is_error
    assert "at least one" in out.content.lower()


@pytest.mark.asyncio
async def test_update_translates_snake_to_camel(ghl_key: str) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"contact": {"id": "c-1"}})

    _install_transport(handler)
    out = await _get("ghl_contact_update").handler(
        {
            "contact_id": "c-1",
            "first_name": "Janet",
            "last_name": "Smith",
            "company_name": "Acme Holdings",
            "tags": ["hot"],
        },
        ToolContext(),
    )
    assert not out.is_error
    assert captured["body"] == {
        "firstName": "Janet",
        "lastName": "Smith",
        "companyName": "Acme Holdings",
        "tags": ["hot"],
    }


# ── ghl_contact_delete ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_requires_contact_id(ghl_key: str) -> None:
    out = await _get("ghl_contact_delete").handler(
        {}, ToolContext(),
    )
    assert out.is_error
    assert "contact_id" in out.content.lower()


@pytest.mark.asyncio
async def test_delete_happy_path(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "DELETE"
        return httpx.Response(200, json={"succeeded": True})

    _install_transport(handler)
    out = await _get("ghl_contact_delete").handler(
        {"contact_id": "c-1"}, ToolContext(),
    )
    assert not out.is_error
    assert out.data["deleted"] is True


# ── ghl_contact_add_tag / remove_tag ─────────────────────────────


@pytest.mark.asyncio
async def test_add_tag_requires_non_empty_tags(ghl_key: str) -> None:
    out = await _get("ghl_contact_add_tag").handler(
        {"contact_id": "c-1", "tags": []}, ToolContext(),
    )
    assert out.is_error
    assert "tags" in out.content.lower()


@pytest.mark.asyncio
async def test_add_tag_drops_empty_strings(ghl_key: str) -> None:
    out = await _get("ghl_contact_add_tag").handler(
        {"contact_id": "c-1", "tags": ["   ", ""]}, ToolContext(),
    )
    assert out.is_error
    assert "non-empty" in out.content.lower()


@pytest.mark.asyncio
async def test_add_tag_happy_path(ghl_key: str) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"tags": ["hot", "lead"]})

    _install_transport(handler)
    out = await _get("ghl_contact_add_tag").handler(
        {"contact_id": "c-1", "tags": ["hot", "lead"]},
        ToolContext(),
    )
    assert not out.is_error
    assert captured["method"] == "POST"
    assert captured["body"] == {"tags": ["hot", "lead"]}
    assert out.data["tags_added"] == ["hot", "lead"]


@pytest.mark.asyncio
async def test_remove_tag_uses_delete_with_body(ghl_key: str) -> None:
    """GHL's DELETE-with-body pattern flows through the tool as
    well — otherwise remove_tag would drop to an empty DELETE + GHL
    would either 400 or silently no-op."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"tags": []})

    _install_transport(handler)
    out = await _get("ghl_contact_remove_tag").handler(
        {"contact_id": "c-1", "tags": ["cold"]},
        ToolContext(),
    )
    assert not out.is_error
    assert captured["method"] == "DELETE"
    assert captured["body"] == {"tags": ["cold"]}


# ── ghl_contact_add_note ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_note_requires_body(ghl_key: str) -> None:
    out = await _get("ghl_contact_add_note").handler(
        {"contact_id": "c-1"}, ToolContext(),
    )
    assert out.is_error
    assert "body" in out.content.lower()


@pytest.mark.asyncio
async def test_note_happy_path_with_user_id(ghl_key: str) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"note": {"id": "n-1"}})

    _install_transport(handler)
    out = await _get("ghl_contact_add_note").handler(
        {
            "contact_id": "c-1",
            "body": "spoke with them at 3pm",
            "user_id": "user-42",
        },
        ToolContext(),
    )
    assert not out.is_error
    assert captured["body"] == {
        "body": "spoke with them at 3pm",
        "userId": "user-42",
    }
    assert out.data["note_id"] == "n-1"


@pytest.mark.asyncio
async def test_note_without_user_id_omits_it(ghl_key: str) -> None:
    """``userId`` is optional — unset it must not appear in the
    body so GHL doesn't resolve it to a ghost user."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"note": {"id": "n-1"}})

    _install_transport(handler)
    await _get("ghl_contact_add_note").handler(
        {"contact_id": "c-1", "body": "just a note"},
        ToolContext(),
    )
    assert "userId" not in captured["body"]
    assert captured["body"]["body"] == "just a note"


# ── ghl_location_list / user_list / custom_field_list ────────────


@pytest.mark.asyncio
async def test_location_list_hits_search(ghl_key: str) -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200,
            json={
                "locations": [
                    {"id": "loc-a", "name": "Acme HQ"},
                    {"id": "loc-b", "name": "Beta Co"},
                ],
            },
        )

    _install_transport(handler)
    out = await _get("ghl_location_list").handler({}, ToolContext())
    assert not out.is_error
    assert captured[0].url.path.endswith("/locations/search")
    assert "Acme HQ" in out.content
    assert "Beta Co" in out.content


@pytest.mark.asyncio
async def test_user_list_scopes_to_default_location(ghl_key: str) -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200,
            json={
                "users": [
                    {
                        "id": "user-1",
                        "firstName": "Alice",
                        "lastName": "A",
                        "email": "a@co.com",
                    },
                ],
            },
        )

    _install_transport(handler)
    out = await _get("ghl_user_list").handler({}, ToolContext())
    assert not out.is_error
    assert captured[0].url.params.get("locationId") == "loc_default"
    assert "Alice" in out.content


@pytest.mark.asyncio
async def test_custom_field_list_hits_location_subpath(
    ghl_key: str,
) -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200,
            json={
                "customFields": [
                    {"id": "f-1", "name": "Referrer", "dataType": "TEXT"},
                ],
            },
        )

    _install_transport(handler)
    out = await _get("ghl_custom_field_list").handler(
        {}, ToolContext(),
    )
    assert not out.is_error
    assert captured[0].url.path.endswith(
        "/locations/loc_default/customFields"
    )
    assert "Referrer" in out.content


# ── error rewrites flow through to contact tools ─────────────────


@pytest.mark.asyncio
async def test_401_rewritten_to_pit_hint(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"message": "Unauthorized"},
        )

    _install_transport(handler)
    out = await _get("ghl_contact_get").handler(
        {"contact_id": "c-1"}, ToolContext(),
    )
    assert out.is_error
    assert "agency pit" in out.content.lower()


@pytest.mark.asyncio
async def test_403_rewritten_to_scope_hint(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"message": "Insufficient scope"},
        )

    _install_transport(handler)
    out = await _get("ghl_contact_get").handler(
        {"contact_id": "c-1"}, ToolContext(),
    )
    assert out.is_error
    assert "scope" in out.content.lower()


@pytest.mark.asyncio
async def test_422_keeps_raw_body_in_data(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "errors": [{"message": "email invalid"}],
            },
        )

    _install_transport(handler)
    out = await _get("ghl_contact_create").handler(
        {"email": "not-an-email"}, ToolContext(),
    )
    assert out.is_error
    assert out.data["status"] == 422
    assert "email invalid" in _json.dumps(out.data["raw"])
