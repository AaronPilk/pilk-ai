"""Tests for GHL calendars + workflows + tasks + tags — PR #75e.

Two tiers:
- :class:`GHLClient` methods round-trip through ``httpx.MockTransport``.
- Tool handlers validate args, convert ISO dates → epoch ms, flow
  GHL errors through the shared rewriter, and defensively parse
  endpoint shape drift (free-slots, tags).

Client tests lock in exact URL + params + body. Tool tests lock in
the planner-facing contract — required fields, date conversion,
at-least-one-field updates, and the flattening of GHL's various
response shapes.
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable

import httpx
import pytest

from core.config import get_settings
from core.integrations.ghl import (
    GHLClient,
    make_ghl_calendar_tools,
    make_ghl_workflow_tools,
)
from core.integrations.ghl.tools import _iso_date_to_ms
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext

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


@pytest.fixture
def ghl_key(monkeypatch) -> str:
    monkeypatch.setenv("GHL_API_KEY", "pit_test")
    monkeypatch.setenv("GHL_DEFAULT_LOCATION_ID", "loc_default")
    get_settings.cache_clear()
    yield "pit_test"
    get_settings.cache_clear()


def _client() -> GHLClient:
    return GHLClient(api_key="pit_test")


def _get(name: str):
    for t in [*make_ghl_calendar_tools(), *make_ghl_workflow_tools()]:
        if t.name == name:
            return t
    raise AssertionError(f"no tool named {name}")


# ── _iso_date_to_ms helper ───────────────────────────────────────


# Midnight UTC 2026-04-21 in epoch ms. Computed once and locked so
# the tests surface any timezone drift in the helper.
_APR_21_2026_UTC_MS = 1_776_729_600_000


def test_iso_date_only_converts_to_midnight_utc_ms() -> None:
    assert _iso_date_to_ms("2026-04-21") == _APR_21_2026_UTC_MS


def test_iso_datetime_with_z_converts() -> None:
    ms = _iso_date_to_ms("2026-04-21T12:00:00Z")
    assert ms == _APR_21_2026_UTC_MS + 12 * 60 * 60 * 1000


def test_iso_datetime_with_offset_converts() -> None:
    # 14:00 PDT (-07:00) = 21:00 UTC → 21 hours after midnight UTC.
    ms = _iso_date_to_ms("2026-04-21T14:00:00-07:00")
    assert ms == _APR_21_2026_UTC_MS + 21 * 60 * 60 * 1000


def test_iso_invalid_raises() -> None:
    with pytest.raises(ValueError):
        _iso_date_to_ms("not-a-date")


# ── client: calendars_list ───────────────────────────────────────


@pytest.mark.asyncio
async def test_client_calendars_list_scopes_to_location() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"calendars": []})

    _install_transport(handler)
    await _client().calendars_list(location_id="loc_1")
    assert captured[0].url.path.endswith("/calendars/")
    assert captured[0].url.params.get("locationId") == "loc_1"


# ── client: free_slots ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_free_slots_sends_ms_params() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"slots": []})

    _install_transport(handler)
    await _client().calendars_free_slots(
        "cal-1",
        start_date_ms=1_000_000,
        end_date_ms=2_000_000,
        timezone="America/New_York",
        user_id="user-1",
    )
    params = captured[0].url.params
    assert captured[0].url.path.endswith("/calendars/cal-1/free-slots")
    assert params.get("startDate") == "1000000"
    assert params.get("endDate") == "2000000"
    assert params.get("timezone") == "America/New_York"
    assert params.get("userId") == "user-1"


# ── client: appointments_list + omit empty ───────────────────────


@pytest.mark.asyncio
async def test_client_appointments_list_omits_empty_filters() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"events": []})

    _install_transport(handler)
    await _client().appointments_list(location_id="loc_1")
    params = captured[0].url.params
    assert params.get("locationId") == "loc_1"
    for k in ("calendarId", "contactId", "userId", "startDate", "endDate"):
        assert k not in params


@pytest.mark.asyncio
async def test_client_appointments_list_attaches_all_filters() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"events": []})

    _install_transport(handler)
    await _client().appointments_list(
        location_id="loc_1",
        calendar_id="cal-1",
        contact_id="c-1",
        user_id="u-1",
        start_date_ms=1_000,
        end_date_ms=2_000,
    )
    params = captured[0].url.params
    assert params.get("calendarId") == "cal-1"
    assert params.get("contactId") == "c-1"
    assert params.get("userId") == "u-1"
    assert params.get("startDate") == "1000"
    assert params.get("endDate") == "2000"


# ── client: appointments_create ──────────────────────────────────


@pytest.mark.asyncio
async def test_client_appointments_create_posts_full_payload() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"appointment": {"id": "a-1"}})

    _install_transport(handler)
    await _client().appointments_create(
        calendar_id="cal-1",
        contact_id="c-1",
        location_id="loc_1",
        start_time_iso="2026-04-21T14:00:00-07:00",
        end_time_iso="2026-04-21T14:30:00-07:00",
        title="intro call",
        appointment_status="confirmed",
        assigned_user_id="u-1",
    )
    body = captured["body"]
    assert captured["method"] == "POST"
    assert captured["path"].endswith("/calendars/events/appointments")
    assert body["calendarId"] == "cal-1"
    assert body["contactId"] == "c-1"
    assert body["locationId"] == "loc_1"
    assert body["startTime"] == "2026-04-21T14:00:00-07:00"
    assert body["endTime"] == "2026-04-21T14:30:00-07:00"
    assert body["title"] == "intro call"
    assert body["appointmentStatus"] == "confirmed"
    assert body["assignedUserId"] == "u-1"


@pytest.mark.asyncio
async def test_client_appointments_update_sends_put() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"appointment": {"id": "a-1"}})

    _install_transport(handler)
    await _client().appointments_update(
        "a-1", payload={"appointmentStatus": "showed"},
    )
    assert captured["method"] == "PUT"
    assert captured["path"].endswith(
        "/calendars/events/appointments/a-1"
    )
    assert captured["body"] == {"appointmentStatus": "showed"}


# ── client: tasks + workflows + tags ─────────────────────────────


@pytest.mark.asyncio
async def test_client_tasks_list_hits_contact_path() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"tasks": []})

    _install_transport(handler)
    await _client().tasks_list("c-1")
    assert captured[0].url.path.endswith("/contacts/c-1/tasks")


@pytest.mark.asyncio
async def test_client_tasks_create_posts_fields() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"task": {"id": "t-1"}})

    _install_transport(handler)
    await _client().tasks_create(
        "c-1",
        title="follow up",
        body="bring pricing sheet",
        due_date_iso="2026-04-22T09:00:00-07:00",
        assigned_to="u-1",
    )
    assert captured["body"] == {
        "title": "follow up",
        "body": "bring pricing sheet",
        "dueDate": "2026-04-22T09:00:00-07:00",
        "assignedTo": "u-1",
    }


@pytest.mark.asyncio
async def test_client_workflows_list_scopes_to_location() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"workflows": []})

    _install_transport(handler)
    await _client().workflows_list(location_id="loc_1")
    assert captured[0].url.path.endswith("/workflows/")
    assert captured[0].url.params.get("locationId") == "loc_1"


@pytest.mark.asyncio
async def test_client_workflows_add_contact_posts_to_nested_path() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["path"] = req.url.path
        captured["body"] = _json.loads(req.content.decode() or "{}")
        return httpx.Response(200, json={"succeeded": True})

    _install_transport(handler)
    # Without event_start_time — body is empty dict.
    await _client().workflows_add_contact(
        contact_id="c-1", workflow_id="w-1",
    )
    assert captured["path"].endswith("/contacts/c-1/workflow/w-1")
    assert captured["body"] == {}
    # With event_start_time
    await _client().workflows_add_contact(
        contact_id="c-1",
        workflow_id="w-1",
        event_start_time_iso="2026-04-22T09:00:00-07:00",
    )
    assert captured["body"] == {
        "eventStartTime": "2026-04-22T09:00:00-07:00"
    }


@pytest.mark.asyncio
async def test_client_tags_list_hits_location_subpath() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"tags": []})

    _install_transport(handler)
    await _client().tags_list(location_id="loc_1")
    assert captured[0].url.path.endswith("/locations/loc_1/tags")


# ── tool surface shape ───────────────────────────────────────────


def test_calendar_factory_emits_five() -> None:
    names = sorted(t.name for t in make_ghl_calendar_tools())
    assert names == sorted([
        "ghl_calendar_list",
        "ghl_appointment_slots",
        "ghl_appointment_list",
        "ghl_appointment_create",
        "ghl_appointment_update",
    ])


def test_workflow_factory_emits_five() -> None:
    names = sorted(t.name for t in make_ghl_workflow_tools())
    assert names == sorted([
        "ghl_task_list",
        "ghl_task_create",
        "ghl_workflow_list",
        "ghl_workflow_add_contact",
        "ghl_tag_list",
    ])


def test_risk_classes() -> None:
    """Every mutation is NET_WRITE; reads are NET_READ. Locks in
    the approval-queue routing — a silent drop would bypass the
    gate for appointment bookings / task creation / workflow
    enrollment."""
    read_names = {
        "ghl_calendar_list",
        "ghl_appointment_slots",
        "ghl_appointment_list",
        "ghl_task_list",
        "ghl_workflow_list",
        "ghl_tag_list",
    }
    for tool in [
        *make_ghl_calendar_tools(),
        *make_ghl_workflow_tools(),
    ]:
        if tool.name in read_names:
            assert tool.risk == RiskClass.NET_READ, tool.name
        else:
            assert tool.risk == RiskClass.NET_WRITE, tool.name


# ── ghl_calendar_list ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_calendar_list_happy_path(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "calendars": [
                    {"id": "cal-1", "name": "Intro Calls", "eventType": "single"},
                    {"id": "cal-2", "name": "Team", "eventType": "team"},
                ],
            },
        )

    _install_transport(handler)
    out = await _get("ghl_calendar_list").handler({}, ToolContext())
    assert not out.is_error
    assert "Intro Calls" in out.content
    assert "Team" in out.content


# ── ghl_appointment_slots ────────────────────────────────────────


@pytest.mark.asyncio
async def test_slots_requires_calendar_id(ghl_key: str) -> None:
    out = await _get("ghl_appointment_slots").handler(
        {"start_date": "2026-04-21", "end_date": "2026-04-22"},
        ToolContext(),
    )
    assert out.is_error
    assert "calendar_id" in out.content.lower()


@pytest.mark.asyncio
async def test_slots_requires_both_dates(ghl_key: str) -> None:
    out = await _get("ghl_appointment_slots").handler(
        {"calendar_id": "cal-1", "start_date": "2026-04-21"},
        ToolContext(),
    )
    assert out.is_error
    assert "end_date" in out.content.lower()


@pytest.mark.asyncio
async def test_slots_rejects_inverted_range(ghl_key: str) -> None:
    out = await _get("ghl_appointment_slots").handler(
        {
            "calendar_id": "cal-1",
            "start_date": "2026-04-22",
            "end_date": "2026-04-21",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "after 'start_date'" in out.content


@pytest.mark.asyncio
async def test_slots_converts_iso_to_ms(ghl_key: str) -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"slots": []})

    _install_transport(handler)
    out = await _get("ghl_appointment_slots").handler(
        {
            "calendar_id": "cal-1",
            "start_date": "2026-04-21",
            "end_date": "2026-04-22",
        },
        ToolContext(),
    )
    assert not out.is_error
    params = captured[0].url.params
    assert params.get("startDate") == str(_APR_21_2026_UTC_MS)
    # Midnight UTC 2026-04-22 = start + 86,400,000 ms.
    assert params.get("endDate") == str(_APR_21_2026_UTC_MS + 86_400_000)


@pytest.mark.asyncio
async def test_slots_handles_nested_response_shape(ghl_key: str) -> None:
    """GHL's free-slots response can be date-keyed
    (``{"2026-04-21": {"slots": [...]}}``) instead of a flat
    ``{"slots": [...]}`` top-level. Locks in that the tool counts +
    previews both shapes without crashing."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "2026-04-21": {
                    "slots": ["09:00", "09:30", "10:00"],
                },
                "2026-04-22": {
                    "slots": ["14:00", "14:30"],
                },
            },
        )

    _install_transport(handler)
    out = await _get("ghl_appointment_slots").handler(
        {
            "calendar_id": "cal-1",
            "start_date": "2026-04-21",
            "end_date": "2026-04-23",
        },
        ToolContext(),
    )
    assert not out.is_error
    assert "5 slot(s)" in out.content


# ── ghl_appointment_create ───────────────────────────────────────


@pytest.mark.asyncio
async def test_create_appointment_requires_calendar_id(
    ghl_key: str,
) -> None:
    out = await _get("ghl_appointment_create").handler(
        {"contact_id": "c-1", "start_time": "2026-04-21T14:00:00-07:00"},
        ToolContext(),
    )
    assert out.is_error
    assert "calendar_id" in out.content.lower()


@pytest.mark.asyncio
async def test_create_appointment_happy_path(ghl_key: str) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(
            200, json={"appointment": {"id": "a-new"}},
        )

    _install_transport(handler)
    out = await _get("ghl_appointment_create").handler(
        {
            "calendar_id": "cal-1",
            "contact_id": "c-1",
            "start_time": "2026-04-21T14:00:00-07:00",
            "end_time": "2026-04-21T14:30:00-07:00",
            "title": "intro call",
        },
        ToolContext(),
    )
    assert not out.is_error
    assert captured["body"]["calendarId"] == "cal-1"
    assert captured["body"]["locationId"] == "loc_default"
    assert out.data["appointment_id"] == "a-new"


# ── ghl_appointment_update ───────────────────────────────────────


@pytest.mark.asyncio
async def test_update_appointment_requires_at_least_one_field(
    ghl_key: str,
) -> None:
    out = await _get("ghl_appointment_update").handler(
        {"appointment_id": "a-1"}, ToolContext(),
    )
    assert out.is_error
    assert "at least one" in out.content.lower()


@pytest.mark.asyncio
async def test_update_appointment_translates_snake_to_camel(
    ghl_key: str,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"appointment": {"id": "a-1"}})

    _install_transport(handler)
    out = await _get("ghl_appointment_update").handler(
        {
            "appointment_id": "a-1",
            "start_time": "2026-04-21T15:00:00-07:00",
            "end_time": "2026-04-21T15:30:00-07:00",
            "appointment_status": "showed",
            "assigned_user_id": "u-2",
            "title": "rescheduled",
        },
        ToolContext(),
    )
    assert not out.is_error
    assert captured["body"] == {
        "startTime": "2026-04-21T15:00:00-07:00",
        "endTime": "2026-04-21T15:30:00-07:00",
        "appointmentStatus": "showed",
        "assignedUserId": "u-2",
        "title": "rescheduled",
    }


# ── ghl_task_list / create ───────────────────────────────────────


@pytest.mark.asyncio
async def test_task_list_requires_contact_id(ghl_key: str) -> None:
    out = await _get("ghl_task_list").handler({}, ToolContext())
    assert out.is_error
    assert "contact_id" in out.content.lower()


@pytest.mark.asyncio
async def test_task_list_renders_checkbox(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tasks": [
                    {
                        "id": "t-1",
                        "title": "follow up",
                        "completed": False,
                        "dueDate": "2026-04-22",
                    },
                    {
                        "id": "t-2",
                        "title": "send quote",
                        "completed": True,
                        "dueDate": "2026-04-20",
                    },
                ],
            },
        )

    _install_transport(handler)
    out = await _get("ghl_task_list").handler(
        {"contact_id": "c-1"}, ToolContext(),
    )
    assert not out.is_error
    assert "[ ] follow up" in out.content
    assert "[x] send quote" in out.content


@pytest.mark.asyncio
async def test_task_create_requires_title(ghl_key: str) -> None:
    out = await _get("ghl_task_create").handler(
        {"contact_id": "c-1"}, ToolContext(),
    )
    assert out.is_error
    assert "title" in out.content.lower()


@pytest.mark.asyncio
async def test_task_create_happy_path(ghl_key: str) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"task": {"id": "t-new"}})

    _install_transport(handler)
    out = await _get("ghl_task_create").handler(
        {
            "contact_id": "c-1",
            "title": "follow up Tuesday",
            "body": "bring the pricing sheet",
            "due_date": "2026-04-22T09:00:00-07:00",
            "assigned_to": "u-1",
        },
        ToolContext(),
    )
    assert not out.is_error
    assert captured["body"] == {
        "title": "follow up Tuesday",
        "body": "bring the pricing sheet",
        "dueDate": "2026-04-22T09:00:00-07:00",
        "assignedTo": "u-1",
    }
    assert out.data["task_id"] == "t-new"


# ── ghl_workflow_list + add_contact ──────────────────────────────


@pytest.mark.asyncio
async def test_workflow_list_happy_path(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "workflows": [
                    {"id": "w-1", "name": "Welcome", "status": "active"},
                    {"id": "w-2", "name": "Re-engage", "status": "draft"},
                ],
            },
        )

    _install_transport(handler)
    out = await _get("ghl_workflow_list").handler({}, ToolContext())
    assert not out.is_error
    assert "Welcome" in out.content
    assert "active" in out.content
    assert "draft" in out.content


@pytest.mark.asyncio
async def test_workflow_add_contact_requires_both_ids(
    ghl_key: str,
) -> None:
    out = await _get("ghl_workflow_add_contact").handler(
        {"contact_id": "c-1"}, ToolContext(),
    )
    assert out.is_error
    assert "workflow_id" in out.content.lower()


@pytest.mark.asyncio
async def test_workflow_add_contact_happy_path(ghl_key: str) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["path"] = req.url.path
        captured["body"] = _json.loads(req.content.decode() or "{}")
        return httpx.Response(200, json={"succeeded": True})

    _install_transport(handler)
    out = await _get("ghl_workflow_add_contact").handler(
        {
            "contact_id": "c-1",
            "workflow_id": "w-1",
            "event_start_time": "2026-04-22T09:00:00-07:00",
        },
        ToolContext(),
    )
    assert not out.is_error
    assert captured["path"].endswith("/contacts/c-1/workflow/w-1")
    assert captured["body"] == {
        "eventStartTime": "2026-04-22T09:00:00-07:00"
    }


# ── ghl_tag_list ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tag_list_dict_shape(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tags": [
                    {"name": "hot-lead"},
                    {"name": "warm"},
                    {"name": "unsubscribed"},
                ],
            },
        )

    _install_transport(handler)
    out = await _get("ghl_tag_list").handler({}, ToolContext())
    assert not out.is_error
    assert out.data["tags"] == ["hot-lead", "warm", "unsubscribed"]


@pytest.mark.asyncio
async def test_tag_list_string_shape(ghl_key: str) -> None:
    """Some GHL endpoints return plain strings instead of
    {name: ...} dicts. Locks in that both shapes decode the same
    way — a planner looking up tags shouldn't care which was
    returned."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"tags": ["hot-lead", "warm"]},
        )

    _install_transport(handler)
    out = await _get("ghl_tag_list").handler({}, ToolContext())
    assert not out.is_error
    assert out.data["tags"] == ["hot-lead", "warm"]


# ── error rewrites + not-configured ──────────────────────────────


@pytest.mark.asyncio
async def test_401_rewritten_to_pit_hint_on_calendar_list(
    ghl_key: str,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    _install_transport(handler)
    out = await _get("ghl_calendar_list").handler({}, ToolContext())
    assert out.is_error
    assert "private integration" in out.content.lower()


@pytest.mark.asyncio
async def test_not_configured_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("GHL_API_KEY", raising=False)
    monkeypatch.delenv("PILK_GHL_API_KEY", raising=False)
    monkeypatch.setenv("GHL_DEFAULT_LOCATION_ID", "loc_default")
    get_settings.cache_clear()
    try:
        out = await _get("ghl_calendar_list").handler(
            {}, ToolContext(),
        )
        assert out.is_error
        assert "not configured" in out.content.lower()
    finally:
        get_settings.cache_clear()
