"""Tests for GHL conversations — PR #75d.

Two tiers:

- :class:`GHLClient` conversation methods round-trip through
  :class:`httpx.MockTransport` so no real GHL calls go out.
- Tool handlers validate their args + surface ``GHLError`` cleanly.

Client tests lock in the exact URL + body the API sees. Tool tests
lock in the planner-facing contract (required fields, risk class,
error copy, defensive parsing of GHL's nested message shape).
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable

import httpx
import pytest

from core.config import get_settings
from core.integrations.ghl import (
    GHLClient,
    make_ghl_conversation_tools,
)
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
    for t in make_ghl_conversation_tools():
        if t.name == name:
            return t
    raise AssertionError(f"no tool named {name}")


# ── client: send_sms ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_send_sms_posts_right_shape() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(
            200,
            json={"conversationId": "conv-1", "messageId": "msg-1"},
        )

    _install_transport(handler)
    await _client().conversations_send_sms(
        contact_id="c-1",
        location_id="loc_1",
        message="hey there",
    )
    assert captured["method"] == "POST"
    assert captured["path"].endswith("/conversations/messages")
    assert captured["body"]["type"] == "SMS"
    assert captured["body"]["contactId"] == "c-1"
    assert captured["body"]["locationId"] == "loc_1"
    assert captured["body"]["message"] == "hey there"
    # fromNumber not passed → must not appear in the body.
    assert "fromNumber" not in captured["body"]


@pytest.mark.asyncio
async def test_client_send_sms_attaches_from_number() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={})

    _install_transport(handler)
    await _client().conversations_send_sms(
        contact_id="c-1",
        location_id="loc_1",
        message="hi",
        from_number="+14155551234",
    )
    assert captured["body"]["fromNumber"] == "+14155551234"


# ── client: send_email ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_send_email_routes_type_and_subject() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={})

    _install_transport(handler)
    await _client().conversations_send_email(
        contact_id="c-1",
        location_id="loc_1",
        subject="hello",
        html="<p>hi</p>",
    )
    body = captured["body"]
    assert body["type"] == "Email"
    assert body["subject"] == "hello"
    assert body["html"] == "<p>hi</p>"
    # Text not set → not in body.
    assert "message" not in body


@pytest.mark.asyncio
async def test_client_send_email_accepts_both_html_and_text() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={})

    _install_transport(handler)
    await _client().conversations_send_email(
        contact_id="c-1",
        location_id="loc_1",
        subject="hello",
        html="<p>hi</p>",
        text="hi",
        from_email="noreply@example.com",
        reply_to="ops@example.com",
    )
    body = captured["body"]
    assert body["html"] == "<p>hi</p>"
    assert body["message"] == "hi"
    assert body["emailFrom"] == "noreply@example.com"
    assert body["replyTo"] == "ops@example.com"


# ── client: conversations_search ─────────────────────────────────


@pytest.mark.asyncio
async def test_client_search_omits_empty_filters() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"conversations": []})

    _install_transport(handler)
    await _client().conversations_search(location_id="loc_1")
    params = captured[0].url.params
    assert params.get("locationId") == "loc_1"
    assert params.get("limit") == "25"
    assert "contactId" not in params
    assert "lastMessageType" not in params


@pytest.mark.asyncio
async def test_client_search_narrows_by_contact_and_type() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"conversations": []})

    _install_transport(handler)
    await _client().conversations_search(
        location_id="loc_1",
        contact_id="c-1",
        last_message_type="SMS",
        limit=5,
    )
    params = captured[0].url.params
    assert params.get("contactId") == "c-1"
    assert params.get("lastMessageType") == "SMS"
    assert params.get("limit") == "5"


# ── client: conversations_get_messages ───────────────────────────


@pytest.mark.asyncio
async def test_client_get_messages_hits_right_path() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"messages": []})

    _install_transport(handler)
    await _client().conversations_get_messages("conv-1", limit=10)
    assert captured[0].url.path.endswith("/conversations/conv-1/messages")
    assert captured[0].url.params.get("limit") == "10"


# ── tool surface shape ───────────────────────────────────────────


def test_factory_emits_four_tools() -> None:
    tools = make_ghl_conversation_tools()
    names = sorted(t.name for t in tools)
    assert names == sorted([
        "ghl_send_sms",
        "ghl_send_email",
        "ghl_conversation_search",
        "ghl_conversation_get_messages",
    ])


def test_risk_classes() -> None:
    """Sends are COMMS (approval queue), reads are NET_READ. Locks
    in the gate routing so an SMS doesn't accidentally drop to a
    lower risk and skip approval."""
    tools = {t.name: t for t in make_ghl_conversation_tools()}
    assert tools["ghl_send_sms"].risk == RiskClass.COMMS
    assert tools["ghl_send_email"].risk == RiskClass.COMMS
    assert tools["ghl_conversation_search"].risk == RiskClass.NET_READ
    assert (
        tools["ghl_conversation_get_messages"].risk
        == RiskClass.NET_READ
    )


# ── ghl_send_sms validation + happy path ─────────────────────────


@pytest.mark.asyncio
async def test_send_sms_requires_contact_id(ghl_key: str) -> None:
    out = await _get("ghl_send_sms").handler(
        {"message": "hi"}, ToolContext(),
    )
    assert out.is_error
    assert "contact_id" in out.content.lower()


@pytest.mark.asyncio
async def test_send_sms_requires_message(ghl_key: str) -> None:
    out = await _get("ghl_send_sms").handler(
        {"contact_id": "c-1"}, ToolContext(),
    )
    assert out.is_error
    assert "message" in out.content.lower()


@pytest.mark.asyncio
async def test_send_sms_caps_length(ghl_key: str) -> None:
    out = await _get("ghl_send_sms").handler(
        {"contact_id": "c-1", "message": "x" * 1601},
        ToolContext(),
    )
    assert out.is_error
    assert "too long" in out.content.lower()


@pytest.mark.asyncio
async def test_send_sms_happy_path(ghl_key: str) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(
            200,
            json={"conversationId": "conv-1", "messageId": "msg-1"},
        )

    _install_transport(handler)
    out = await _get("ghl_send_sms").handler(
        {"contact_id": "c-1", "message": "hey"},
        ToolContext(),
    )
    assert not out.is_error
    body = captured["body"]
    assert body["type"] == "SMS"
    assert body["contactId"] == "c-1"
    assert body["message"] == "hey"
    assert body["locationId"] == "loc_default"
    assert out.data["conversation_id"] == "conv-1"
    assert out.data["message_id"] == "msg-1"


# ── ghl_send_email validation + happy path ───────────────────────


@pytest.mark.asyncio
async def test_send_email_requires_contact_id(ghl_key: str) -> None:
    out = await _get("ghl_send_email").handler(
        {"subject": "x", "text": "y"}, ToolContext(),
    )
    assert out.is_error
    assert "contact_id" in out.content.lower()


@pytest.mark.asyncio
async def test_send_email_requires_subject(ghl_key: str) -> None:
    out = await _get("ghl_send_email").handler(
        {"contact_id": "c-1", "text": "y"}, ToolContext(),
    )
    assert out.is_error
    assert "subject" in out.content.lower()


@pytest.mark.asyncio
async def test_send_email_requires_html_or_text(ghl_key: str) -> None:
    """Neither html nor text → clean error before the send. Locks in
    the 'at least one body' contract so the planner can't accidentally
    fire an empty email."""
    out = await _get("ghl_send_email").handler(
        {"contact_id": "c-1", "subject": "hi"},
        ToolContext(),
    )
    assert out.is_error
    assert "html" in out.content.lower()
    assert "text" in out.content.lower()


@pytest.mark.asyncio
async def test_send_email_happy_path_with_text_only(
    ghl_key: str,
) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(
            200,
            json={"conversationId": "conv-1", "messageId": "msg-1"},
        )

    _install_transport(handler)
    out = await _get("ghl_send_email").handler(
        {
            "contact_id": "c-1",
            "subject": "Follow-up",
            "text": "thanks for your time",
        },
        ToolContext(),
    )
    assert not out.is_error
    body = captured["body"]
    assert body["type"] == "Email"
    assert body["subject"] == "Follow-up"
    assert body["message"] == "thanks for your time"
    assert "html" not in body


# ── ghl_conversation_search ──────────────────────────────────────


@pytest.mark.asyncio
async def test_search_happy_path(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "conversations": [
                    {
                        "id": "conv-1",
                        "lastMessageBody": "thanks for the call",
                        "lastMessageType": "SMS",
                        "lastMessageDate": "2026-04-21T12:00",
                    },
                    {
                        "id": "conv-2",
                        "lastMessageBody": "see you Monday",
                        "lastMessageType": "Email",
                        "lastMessageDate": "2026-04-20T08:00",
                    },
                ],
                "total": 2,
            },
        )

    _install_transport(handler)
    out = await _get("ghl_conversation_search").handler(
        {}, ToolContext(),
    )
    assert not out.is_error
    assert out.data["total"] == 2
    assert "thanks for the call" in out.content
    assert "SMS" in out.content
    assert "Email" in out.content


@pytest.mark.asyncio
async def test_search_clamps_oversize_limit(ghl_key: str) -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"conversations": []})

    _install_transport(handler)
    await _get("ghl_conversation_search").handler(
        {"limit": 9999}, ToolContext(),
    )
    assert captured[0].url.params.get("limit") == "100"


# ── ghl_conversation_get_messages ────────────────────────────────


@pytest.mark.asyncio
async def test_get_messages_requires_conversation_id(
    ghl_key: str,
) -> None:
    out = await _get("ghl_conversation_get_messages").handler(
        {}, ToolContext(),
    )
    assert out.is_error
    assert "conversation_id" in out.content.lower()


@pytest.mark.asyncio
async def test_get_messages_happy_path(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "messages": [
                    {
                        "id": "m-1",
                        "dateAdded": "2026-04-21T12:00",
                        "type": "SMS",
                        "direction": "outbound",
                        "body": "hey there",
                    },
                    {
                        "id": "m-2",
                        "dateAdded": "2026-04-21T12:05",
                        "type": "SMS",
                        "direction": "inbound",
                        "body": "thanks!",
                    },
                ],
            },
        )

    _install_transport(handler)
    out = await _get("ghl_conversation_get_messages").handler(
        {"conversation_id": "conv-1"}, ToolContext(),
    )
    assert not out.is_error
    assert len(out.data["messages"]) == 2
    # Direction arrow renders in content: outbound → / inbound ←.
    assert "→" in out.content
    assert "←" in out.content


@pytest.mark.asyncio
async def test_get_messages_flattens_nested_shape(
    ghl_key: str,
) -> None:
    """GHL sometimes returns ``{"messages": {"messages": [...]}}``
    instead of ``{"messages": [...]}`` (v1 vs v2 endpoint drift).
    Locks in the defensive flatten so the tool works across both
    shapes."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "messages": {
                    "messages": [
                        {
                            "id": "m-1",
                            "type": "SMS",
                            "direction": "outbound",
                            "body": "hey",
                            "dateAdded": "2026-04-21T12:00",
                        },
                    ],
                    "nextPage": False,
                },
            },
        )

    _install_transport(handler)
    out = await _get("ghl_conversation_get_messages").handler(
        {"conversation_id": "conv-1"}, ToolContext(),
    )
    assert not out.is_error
    assert len(out.data["messages"]) == 1
    assert "hey" in out.content


# ── error rewrites ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_sms_401_rewritten_to_pit_hint(
    ghl_key: str,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"message": "Unauthorized"},
        )

    _install_transport(handler)
    out = await _get("ghl_send_sms").handler(
        {"contact_id": "c-1", "message": "hi"},
        ToolContext(),
    )
    assert out.is_error
    assert "agency pit" in out.content.lower()


@pytest.mark.asyncio
async def test_send_email_422_keeps_raw_body(ghl_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"errors": [{"message": "subject too long"}]},
        )

    _install_transport(handler)
    out = await _get("ghl_send_email").handler(
        {
            "contact_id": "c-1",
            "subject": "x" * 999,
            "text": "body",
        },
        ToolContext(),
    )
    assert out.is_error
    assert out.data["status"] == 422
    assert "subject too long" in _json.dumps(out.data["raw"])


# ── not-configured guard ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_sms_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("GHL_API_KEY", raising=False)
    monkeypatch.delenv("PILK_GHL_API_KEY", raising=False)
    monkeypatch.setenv("GHL_DEFAULT_LOCATION_ID", "loc_default")
    get_settings.cache_clear()
    try:
        out = await _get("ghl_send_sms").handler(
            {"contact_id": "c-1", "message": "hi"},
            ToolContext(),
        )
        assert out.is_error
        assert "not configured" in out.content.lower()
    finally:
        get_settings.cache_clear()
