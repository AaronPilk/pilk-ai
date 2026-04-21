"""Tests for the Go High Level HTTP client — foundation (PR #75a).

Every round-trip mocked via ``httpx.MockTransport`` so no real GHL
calls go out. Three tiers:

- :func:`resolve_location_id` picks the right id (arg → default → raise)
- :class:`GHLClient` contacts endpoints send the right shape + decode
- Error decode lifts ``{"message": ...}`` + ``{"errors":[{"message": ...}]}``
  into :class:`GHLError`

Follow-up PRs (#75b+) will add tool-handler tests; the client-level
tests here are the contract every tool builds on top of.
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable

import httpx
import pytest

from core.config import get_settings
from core.integrations.ghl import (
    GHL_API_VERSION,
    GHLClient,
    GHLError,
    GHLNotConfiguredError,
    client_from_settings,
    resolve_location_id,
)

# ── httpx mock plumbing ──────────────────────────────────────────


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


def _client() -> GHLClient:
    return GHLClient(api_key="pit_test")


# ── resolve_location_id ──────────────────────────────────────────


def test_resolve_prefers_explicit_arg() -> None:
    assert resolve_location_id(arg="loc_arg", default="loc_default") == "loc_arg"


def test_resolve_falls_back_to_default() -> None:
    assert resolve_location_id(arg=None, default="loc_default") == "loc_default"


def test_resolve_empty_arg_falls_back() -> None:
    """An empty-string arg shouldn't beat the default — treat it the
    same as "not provided" so a planner passing ``""`` doesn't blow
    up the call."""
    assert resolve_location_id(arg="", default="loc_default") == "loc_default"


def test_resolve_raises_when_none_available() -> None:
    with pytest.raises(GHLError) as info:
        resolve_location_id(arg=None, default=None)
    assert info.value.status == 400
    assert "location_id" in info.value.message


def test_resolve_strips_whitespace() -> None:
    assert resolve_location_id(arg="  loc  ", default=None) == "loc"


# ── auth + versioning headers ────────────────────────────────────


@pytest.mark.asyncio
async def test_contacts_get_sends_auth_and_version_headers() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"contact": {"id": "c1"}})

    _install_transport(handler)
    await _client().contacts_get("c1")
    req = captured[0]
    assert req.headers["Authorization"] == "Bearer pit_test"
    assert req.headers["Version"] == GHL_API_VERSION
    assert req.url.path.endswith("/contacts/c1")


# ── contacts_create ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_contacts_create_attaches_location_id() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        captured["method"] = req.method
        captured["path"] = req.url.path
        return httpx.Response(200, json={"contact": {"id": "c-new"}})

    _install_transport(handler)
    result = await _client().contacts_create(
        location_id="loc_1",
        payload={
            "firstName": "Jane",
            "email": "jane@example.com",
        },
    )
    assert result["contact"]["id"] == "c-new"
    assert captured["method"] == "POST"
    assert captured["path"].endswith("/contacts/")
    assert captured["body"]["locationId"] == "loc_1"
    assert captured["body"]["firstName"] == "Jane"
    assert captured["body"]["email"] == "jane@example.com"


# ── contacts_search routes email / phone / query ─────────────────


@pytest.mark.asyncio
async def test_contacts_search_by_email_sends_email_param() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"contacts": []})

    _install_transport(handler)
    await _client().contacts_search(
        location_id="loc_1", email="jane@example.com",
    )
    params = captured[0].url.params
    assert params.get("email") == "jane@example.com"
    assert params.get("locationId") == "loc_1"
    # Query-by-string must NOT be sent when email is specified.
    assert "query" not in params


@pytest.mark.asyncio
async def test_contacts_search_by_phone_sends_phone_param() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"contacts": []})

    _install_transport(handler)
    await _client().contacts_search(
        location_id="loc_1", phone="+14155551234",
    )
    params = captured[0].url.params
    assert params.get("phone") == "+14155551234"


@pytest.mark.asyncio
async def test_contacts_search_by_query_sends_query_param() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"contacts": []})

    _install_transport(handler)
    await _client().contacts_search(location_id="loc_1", query="acme")
    params = captured[0].url.params
    assert params.get("query") == "acme"


@pytest.mark.asyncio
async def test_contacts_search_email_beats_phone_and_query() -> None:
    """Locks in the exact-match-over-fuzzy precedence. If both
    email and query are given, email wins — the test fails loudly
    if a future refactor silently switches the order."""
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"contacts": []})

    _install_transport(handler)
    await _client().contacts_search(
        location_id="loc_1",
        email="jane@example.com",
        phone="+14155551234",
        query="should be ignored",
    )
    params = captured[0].url.params
    assert params.get("email") == "jane@example.com"
    assert "query" not in params


# ── contacts_update + delete ─────────────────────────────────────


@pytest.mark.asyncio
async def test_contacts_update_sends_put_to_contact_path() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"contact": {"id": "c1"}})

    _install_transport(handler)
    await _client().contacts_update(
        "c1", payload={"firstName": "Janet"},
    )
    assert captured["method"] == "PUT"
    assert captured["path"].endswith("/contacts/c1")
    assert captured["body"]["firstName"] == "Janet"


@pytest.mark.asyncio
async def test_contacts_delete_sends_delete() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        return httpx.Response(200, json={"succeded": True})

    _install_transport(handler)
    await _client().contacts_delete("c1")
    assert captured["method"] == "DELETE"
    assert captured["path"].endswith("/contacts/c1")


# ── tag add / remove ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_contacts_add_tags_posts_tags_array() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"tags": ["hot"]})

    _install_transport(handler)
    await _client().contacts_add_tags("c1", tags=["hot", "pr-lead"])
    assert captured["method"] == "POST"
    assert captured["path"].endswith("/contacts/c1/tags")
    assert captured["body"] == {"tags": ["hot", "pr-lead"]}


@pytest.mark.asyncio
async def test_contacts_remove_tags_sends_delete_with_body() -> None:
    """httpx's default .delete() doesn't take a json body; the
    client uses .request('DELETE', …) to support GHL's non-standard
    DELETE-with-body shape."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"tags": []})

    _install_transport(handler)
    await _client().contacts_remove_tags("c1", tags=["cold"])
    assert captured["method"] == "DELETE"
    assert captured["path"].endswith("/contacts/c1/tags")
    assert captured["body"] == {"tags": ["cold"]}


# ── notes ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_contacts_add_note_body_only() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"note": {"id": "n1"}})

    _install_transport(handler)
    await _client().contacts_add_note("c1", body="left a voicemail")
    assert captured["body"] == {"body": "left a voicemail"}


@pytest.mark.asyncio
async def test_contacts_add_note_attaches_user_id_when_set() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"note": {"id": "n1"}})

    _install_transport(handler)
    await _client().contacts_add_note(
        "c1", body="hi", user_id="user_42",
    )
    assert captured["body"] == {"body": "hi", "userId": "user_42"}


# ── meta reads ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_locations_list_hits_search_endpoint() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"locations": []})

    _install_transport(handler)
    await _client().locations_list()
    assert captured[0].url.path.endswith("/locations/search")


@pytest.mark.asyncio
async def test_locations_list_narrows_by_company_id() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"locations": []})

    _install_transport(handler)
    await _client().locations_list(company_id="comp_abc")
    assert captured[0].url.params.get("companyId") == "comp_abc"


@pytest.mark.asyncio
async def test_users_list_scopes_to_location() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"users": []})

    _install_transport(handler)
    await _client().users_list(location_id="loc_1")
    assert captured[0].url.params.get("locationId") == "loc_1"


@pytest.mark.asyncio
async def test_custom_fields_list_hits_location_subpath() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"customFields": []})

    _install_transport(handler)
    await _client().custom_fields_list(location_id="loc_1")
    assert captured[0].url.path.endswith(
        "/locations/loc_1/customFields"
    )


# ── error decode ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_error_with_message_field() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"message": "Contact not found"},
        )

    _install_transport(handler)
    with pytest.raises(GHLError) as info:
        await _client().contacts_get("nope")
    assert info.value.status == 404
    assert info.value.message == "Contact not found"


@pytest.mark.asyncio
async def test_error_with_errors_array_lifts_first_message() -> None:
    """GHL's validation endpoints return an ``errors`` array instead
    of a top-level message. Locks in that we extract the first
    item's message rather than falling back to "HTTP 422"."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "errors": [
                    {"message": "email is required"},
                    {"message": "phone format invalid"},
                ],
            },
        )

    _install_transport(handler)
    with pytest.raises(GHLError) as info:
        await _client().contacts_get("whatever")
    assert info.value.status == 422
    assert info.value.message == "email is required"


@pytest.mark.asyncio
async def test_error_with_empty_body_falls_back_to_status() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _install_transport(handler)
    with pytest.raises(GHLError) as info:
        await _client().contacts_get("x")
    assert info.value.status == 500
    assert "HTTP 500" in info.value.message


@pytest.mark.asyncio
async def test_non_json_error_surfaces_cleanly() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            content=b"<html>gateway error</html>",
            headers={"content-type": "text/html"},
        )

    _install_transport(handler)
    with pytest.raises(GHLError) as info:
        await _client().contacts_get("x")
    assert info.value.status == 502
    assert "non-JSON" in info.value.message


# ── client_from_settings ─────────────────────────────────────────


def test_client_from_settings_requires_api_key(monkeypatch) -> None:
    """No ghl_api_key set anywhere → GHLNotConfiguredError so tool
    handlers can surface a friendly "add the key in Settings" hint."""
    monkeypatch.delenv("GHL_API_KEY", raising=False)
    monkeypatch.delenv("PILK_GHL_API_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(GHLNotConfiguredError):
        client_from_settings()
    get_settings.cache_clear()


def test_client_from_settings_builds_when_env_set(monkeypatch) -> None:
    monkeypatch.setenv("GHL_API_KEY", "pit_from_env")
    get_settings.cache_clear()
    try:
        client = client_from_settings()
        assert isinstance(client, GHLClient)
    finally:
        get_settings.cache_clear()
