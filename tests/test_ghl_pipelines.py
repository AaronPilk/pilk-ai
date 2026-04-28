"""Tests for GHL pipelines + opportunities — PR #75b.

Two tiers:

- :class:`GHLClient` pipeline / opportunity methods round-trip
  through :class:`httpx.MockTransport` so no real GHL calls go out.
- Tool handlers validate their args + surface ``GHLError`` cleanly.

Client tests lock in the exact URL shape + body payload sent to
GHL. Tool tests lock in the planner-facing contract (required
fields, risk class, error copy). Keeping the two separable means
changes to GHL's endpoint can't silently drift the tool surface
and vice versa.
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable

import httpx
import pytest

from core.config import get_settings
from core.integrations.ghl import (
    GHLClient,
    GHLError,
    make_ghl_pipeline_tools,
)
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext

# ── httpx mock plumbing (mirrors test_ghl_client.py) ─────────────


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
    """Set ``GHL_API_KEY`` env var for the duration of the test so
    ``client_from_settings`` succeeds inside tool handlers.
    Also seed a default location so location-scoped tools don't
    have to pass one explicitly."""
    monkeypatch.setenv("GHL_API_KEY", "pit_test")
    monkeypatch.setenv("GHL_DEFAULT_LOCATION_ID", "loc_default")
    get_settings.cache_clear()
    yield "pit_test"
    get_settings.cache_clear()


def _client() -> GHLClient:
    return GHLClient(api_key="pit_test")


def _get(name: str):
    for t in make_ghl_pipeline_tools():
        if t.name == name:
            return t
    raise AssertionError(f"no tool named {name}")


# ── client: pipelines_list ───────────────────────────────────────


@pytest.mark.asyncio
async def test_client_pipelines_list_scopes_to_location() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"pipelines": []})

    _install_transport(handler)
    await _client().pipelines_list(location_id="loc_1")
    assert captured[0].url.path.endswith("/opportunities/pipelines")
    assert captured[0].url.params.get("locationId") == "loc_1"


# ── client: opportunities_create ─────────────────────────────────


@pytest.mark.asyncio
async def test_client_opportunities_create_attaches_location_id() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        captured["method"] = req.method
        captured["path"] = req.url.path
        return httpx.Response(200, json={"opportunity": {"id": "o-new"}})

    _install_transport(handler)
    await _client().opportunities_create(
        location_id="loc_1",
        payload={
            "pipelineId": "p-1",
            "pipelineStageId": "s-1",
            "contactId": "c-1",
            "name": "Acme - website",
        },
    )
    assert captured["method"] == "POST"
    assert captured["path"].endswith("/opportunities/")
    assert captured["body"]["locationId"] == "loc_1"
    assert captured["body"]["pipelineId"] == "p-1"
    assert captured["body"]["name"] == "Acme - website"


# ── client: opportunities_search filter routing ──────────────────


@pytest.mark.asyncio
async def test_client_search_sends_every_filter_set() -> None:
    """Every unset filter must be omitted so GHL sees a clean
    query string (not ``&status=&pipeline_id=`` garbage)."""
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"opportunities": []})

    _install_transport(handler)
    await _client().opportunities_search(
        location_id="loc_1",
        query="acme",
        pipeline_id="p-1",
        status="open",
        limit=10,
    )
    params = captured[0].url.params
    assert params.get("location_id") == "loc_1"
    assert params.get("query") == "acme"
    assert params.get("pipeline_id") == "p-1"
    assert params.get("status") == "open"
    assert params.get("limit") == "10"
    # Unset filters must not be in the query string.
    assert "pipeline_stage_id" not in params
    assert "assigned_to" not in params


@pytest.mark.asyncio
async def test_client_search_omits_empty_filters() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"opportunities": []})

    _install_transport(handler)
    await _client().opportunities_search(location_id="loc_1")
    params = captured[0].url.params
    # Only location_id + limit present.
    assert "query" not in params
    assert "status" not in params
    assert "pipeline_id" not in params


# ── client: update + delete ──────────────────────────────────────


@pytest.mark.asyncio
async def test_client_update_sends_put() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"opportunity": {"id": "o-1"}})

    _install_transport(handler)
    await _client().opportunities_update(
        "o-1", payload={"name": "new name"},
    )
    assert captured["method"] == "PUT"
    assert captured["path"].endswith("/opportunities/o-1")
    assert captured["body"] == {"name": "new name"}


@pytest.mark.asyncio
async def test_client_move_stage_delegates_to_update() -> None:
    """``opportunities_move_stage`` is a shortcut over ``_update``.
    Locks in that the shortcut posts the right fields + keeps
    pipeline_id opt-in (GHL sometimes 422s when moving across
    pipelines without it)."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        captured["path"] = req.url.path
        return httpx.Response(200, json={"opportunity": {"id": "o-1"}})

    _install_transport(handler)
    # Without pipeline_id
    await _client().opportunities_move_stage(
        "o-1", pipeline_stage_id="s-new",
    )
    assert captured["body"] == {"pipelineStageId": "s-new"}
    # With pipeline_id
    await _client().opportunities_move_stage(
        "o-1", pipeline_stage_id="s-new", pipeline_id="p-new",
    )
    assert captured["body"] == {
        "pipelineStageId": "s-new",
        "pipelineId": "p-new",
    }


@pytest.mark.asyncio
async def test_client_delete_sends_delete() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        return httpx.Response(200, json={"succeeded": True})

    _install_transport(handler)
    await _client().opportunities_delete("o-1")
    assert captured["method"] == "DELETE"
    assert captured["path"].endswith("/opportunities/o-1")


# ── tool surface shape ───────────────────────────────────────────


def test_tool_factory_emits_all_seven() -> None:
    tools = make_ghl_pipeline_tools()
    names = sorted(t.name for t in tools)
    assert names == sorted([
        "ghl_pipeline_list",
        "ghl_opportunity_create",
        "ghl_opportunity_get",
        "ghl_opportunity_search",
        "ghl_opportunity_update",
        "ghl_opportunity_move_stage",
        "ghl_opportunity_delete",
    ])


def test_risk_classes() -> None:
    read_names = {"ghl_pipeline_list", "ghl_opportunity_get", "ghl_opportunity_search"}
    for tool in make_ghl_pipeline_tools():
        if tool.name in read_names:
            assert tool.risk == RiskClass.NET_READ, tool.name
        else:
            assert tool.risk == RiskClass.NET_WRITE, tool.name


# ── tool handler: not-configured ─────────────────────────────────


@pytest.mark.asyncio
async def test_tool_reports_not_configured_without_api_key(
    monkeypatch,
) -> None:
    monkeypatch.delenv("GHL_API_KEY", raising=False)
    monkeypatch.delenv("PILK_GHL_API_KEY", raising=False)
    monkeypatch.setenv("GHL_DEFAULT_LOCATION_ID", "loc_default")
    get_settings.cache_clear()
    try:
        out = await _get("ghl_pipeline_list").handler({}, ToolContext())
        assert out.is_error
        assert "not configured" in out.content.lower()
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_tool_reports_missing_location(monkeypatch) -> None:
    """With no location arg and no default, the tool fails loudly
    before it ever tries to hit the network."""
    monkeypatch.setenv("GHL_API_KEY", "pit_test")
    monkeypatch.delenv("GHL_DEFAULT_LOCATION_ID", raising=False)
    monkeypatch.delenv("PILK_GHL_DEFAULT_LOCATION_ID", raising=False)
    get_settings.cache_clear()
    try:
        out = await _get("ghl_pipeline_list").handler({}, ToolContext())
        assert out.is_error
        assert "location_id" in out.content.lower()
    finally:
        get_settings.cache_clear()


# ── tool: ghl_pipeline_list happy path ───────────────────────────


@pytest.mark.asyncio
async def test_pipeline_list_renders_stages_in_order(
    ghl_key: str,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "pipelines": [
                    {
                        "id": "p-1",
                        "name": "Sales",
                        "stages": [
                            {"id": "s-aaa1", "name": "New"},
                            {"id": "s-bbb2", "name": "Qualified"},
                            {"id": "s-ccc3", "name": "Won"},
                        ],
                    },
                ],
            },
        )

    _install_transport(handler)
    out = await _get("ghl_pipeline_list").handler(
        {}, ToolContext(),
    )
    assert not out.is_error
    assert "Sales" in out.content
    # Stages render in order separated by → so the planner sees the flow.
    assert "New" in out.content
    assert "Qualified" in out.content
    assert "Won" in out.content
    assert " → " in out.content
    assert out.data["location_id"] == "loc_default"


# ── tool: ghl_opportunity_create validation + happy path ─────────


@pytest.mark.asyncio
async def test_create_requires_pipeline_id(ghl_key: str) -> None:
    out = await _get("ghl_opportunity_create").handler(
        {
            "pipeline_stage_id": "s-1",
            "contact_id": "c-1",
            "name": "x",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "pipeline_id" in out.content


@pytest.mark.asyncio
async def test_create_requires_pipeline_stage_id(ghl_key: str) -> None:
    out = await _get("ghl_opportunity_create").handler(
        {
            "pipeline_id": "p-1",
            "contact_id": "c-1",
            "name": "x",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "pipeline_stage_id" in out.content


@pytest.mark.asyncio
async def test_create_requires_contact_id(ghl_key: str) -> None:
    out = await _get("ghl_opportunity_create").handler(
        {
            "pipeline_id": "p-1",
            "pipeline_stage_id": "s-1",
            "name": "x",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "contact_id" in out.content


@pytest.mark.asyncio
async def test_create_requires_name(ghl_key: str) -> None:
    out = await _get("ghl_opportunity_create").handler(
        {
            "pipeline_id": "p-1",
            "pipeline_stage_id": "s-1",
            "contact_id": "c-1",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "name" in out.content


@pytest.mark.asyncio
async def test_create_happy_path_sends_full_payload(
    ghl_key: str,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(
            200, json={"opportunity": {"id": "o-new"}},
        )

    _install_transport(handler)
    out = await _get("ghl_opportunity_create").handler(
        {
            "pipeline_id": "p-1",
            "pipeline_stage_id": "s-1",
            "contact_id": "c-1",
            "name": "Acme - website rebuild",
            "monetary_value": 5000,
            "status": "open",
            "assigned_to": "user-99",
        },
        ToolContext(),
    )
    assert not out.is_error
    assert out.data["opportunity_id"] == "o-new"
    body = captured["body"]
    assert body["pipelineId"] == "p-1"
    assert body["pipelineStageId"] == "s-1"
    assert body["contactId"] == "c-1"
    assert body["name"] == "Acme - website rebuild"
    assert body["monetaryValue"] == 5000
    assert body["status"] == "open"
    assert body["assignedTo"] == "user-99"
    assert body["locationId"] == "loc_default"


# ── tool: ghl_opportunity_get happy + missing ────────────────────


@pytest.mark.asyncio
async def test_get_requires_opportunity_id(ghl_key: str) -> None:
    out = await _get("ghl_opportunity_get").handler({}, ToolContext())
    assert out.is_error
    assert "opportunity_id" in out.content


@pytest.mark.asyncio
async def test_get_happy_path(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "opportunity": {
                    "id": "o-1",
                    "name": "Acme deal",
                    "status": "open",
                    "pipelineStageId": "s-xxx1",
                    "monetaryValue": 2500,
                },
            },
        )

    _install_transport(handler)
    out = await _get("ghl_opportunity_get").handler(
        {"opportunity_id": "o-1"}, ToolContext(),
    )
    assert not out.is_error
    assert "Acme deal" in out.content
    assert "open" in out.content
    assert "2500" in out.content


# ── tool: ghl_opportunity_search ─────────────────────────────────


@pytest.mark.asyncio
async def test_search_happy_path(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "opportunities": [
                    {
                        "id": "o-1",
                        "name": "Acme",
                        "status": "open",
                        "monetaryValue": 5000,
                    },
                    {
                        "id": "o-2",
                        "name": "Beta Co",
                        "status": "won",
                        "monetaryValue": 12000,
                    },
                ],
                "total": 2,
            },
        )

    _install_transport(handler)
    out = await _get("ghl_opportunity_search").handler(
        {"query": "a"}, ToolContext(),
    )
    assert not out.is_error
    assert len(out.data["opportunities"]) == 2
    assert "Acme" in out.content
    assert "Beta Co" in out.content
    assert out.data["total"] == 2


@pytest.mark.asyncio
async def test_search_clamps_oversize_limit(ghl_key: str) -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"opportunities": []})

    _install_transport(handler)
    await _get("ghl_opportunity_search").handler(
        {"limit": 9999}, ToolContext(),
    )
    assert captured[0].url.params.get("limit") == "100"


# ── tool: ghl_opportunity_update ─────────────────────────────────


@pytest.mark.asyncio
async def test_update_requires_opportunity_id(ghl_key: str) -> None:
    out = await _get("ghl_opportunity_update").handler(
        {"name": "new name"}, ToolContext(),
    )
    assert out.is_error
    assert "opportunity_id" in out.content


@pytest.mark.asyncio
async def test_update_requires_at_least_one_field(
    ghl_key: str,
) -> None:
    out = await _get("ghl_opportunity_update").handler(
        {"opportunity_id": "o-1"}, ToolContext(),
    )
    assert out.is_error
    assert "at least one" in out.content.lower()


@pytest.mark.asyncio
async def test_update_translates_snake_to_camel(ghl_key: str) -> None:
    """Planner passes snake_case; GHL expects camelCase. Locks in
    the translation so a future refactor doesn't silently drop a
    field."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"opportunity": {"id": "o-1"}})

    _install_transport(handler)
    out = await _get("ghl_opportunity_update").handler(
        {
            "opportunity_id": "o-1",
            "name": "renamed",
            "status": "won",
            "pipeline_stage_id": "s-new",
            "assigned_to": "u-9",
            "monetary_value": 7500,
        },
        ToolContext(),
    )
    assert not out.is_error
    assert captured["body"] == {
        "name": "renamed",
        "status": "won",
        "pipelineStageId": "s-new",
        "assignedTo": "u-9",
        "monetaryValue": 7500,
    }


# ── tool: ghl_opportunity_move_stage ─────────────────────────────


@pytest.mark.asyncio
async def test_move_stage_requires_both_ids(ghl_key: str) -> None:
    out = await _get("ghl_opportunity_move_stage").handler(
        {"opportunity_id": "o-1"}, ToolContext(),
    )
    assert out.is_error
    assert "pipeline_stage_id" in out.content


@pytest.mark.asyncio
async def test_move_stage_sends_only_stage_when_pipeline_omitted(
    ghl_key: str,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"opportunity": {"id": "o-1"}})

    _install_transport(handler)
    out = await _get("ghl_opportunity_move_stage").handler(
        {
            "opportunity_id": "o-1",
            "pipeline_stage_id": "s-new",
        },
        ToolContext(),
    )
    assert not out.is_error
    assert captured["body"] == {"pipelineStageId": "s-new"}


# ── tool: ghl_opportunity_delete ─────────────────────────────────


@pytest.mark.asyncio
async def test_delete_requires_opportunity_id(ghl_key: str) -> None:
    out = await _get("ghl_opportunity_delete").handler(
        {}, ToolContext(),
    )
    assert out.is_error
    assert "opportunity_id" in out.content


@pytest.mark.asyncio
async def test_delete_happy_path(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "DELETE"
        return httpx.Response(200, json={"succeeded": True})

    _install_transport(handler)
    out = await _get("ghl_opportunity_delete").handler(
        {"opportunity_id": "o-1"}, ToolContext(),
    )
    assert not out.is_error
    assert out.data["deleted"] is True


# ── tool: error rewrite ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_401_rewritten_to_pit_hint(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"message": "Unauthorized"},
        )

    _install_transport(handler)
    out = await _get("ghl_pipeline_list").handler({}, ToolContext())
    assert out.is_error
    assert "private integration" in out.content.lower()


@pytest.mark.asyncio
async def test_403_rewritten_to_scope_hint(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": "Insufficient scope"},
        )

    _install_transport(handler)
    out = await _get("ghl_pipeline_list").handler({}, ToolContext())
    assert out.is_error
    assert "scope" in out.content.lower()


@pytest.mark.asyncio
async def test_429_rewritten_to_rate_limit_hint(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"message": "Too many requests"},
        )

    _install_transport(handler)
    out = await _get("ghl_pipeline_list").handler({}, ToolContext())
    assert out.is_error
    assert "rate limit" in out.content.lower()


# ── client errors flow up through tools ──────────────────────────


@pytest.mark.asyncio
async def test_client_error_surfaces_raw_in_data(ghl_key: str) -> None:
    """Tool error surfaces hint as content but keeps the raw body
    in data.raw so debugging doesn't lose context."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "errors": [{"message": "pipelineId required"}],
            },
        )

    _install_transport(handler)
    out = await _get("ghl_opportunity_search").handler(
        {}, ToolContext(),
    )
    assert out.is_error
    assert out.data["status"] == 422
    assert "pipelineId required" in _json.dumps(out.data["raw"])


# ── sanity: client-level GHLError re-raised in client tests ──────


@pytest.mark.asyncio
async def test_client_pipelines_list_raises_on_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"message": "Not found"},
        )

    _install_transport(handler)
    with pytest.raises(GHLError) as info:
        await _client().pipelines_list(location_id="loc_1")
    assert info.value.status == 404
