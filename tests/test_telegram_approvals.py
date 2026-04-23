"""Tests for the Telegram ↔ approvals bridge.

Covers the two flows that make the bridge useful:
  1. An ``approval.created`` event on the hub turns into a Telegram
     sendMessage with inline Approve / Reject buttons.
  2. A ``callback_query`` update routes through ``handle_callback``
     into ``ApprovalManager.approve`` / ``reject`` with the right
     decision, and the card gets rewritten in place.

Stubs ``httpx`` via ``MockTransport`` (matching test_telegram_bridge.py)
so no real network call goes out.
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from core.api.hub import Hub
from core.config import get_settings
from core.db import ensure_schema
from core.integrations.telegram import TelegramClient, TelegramConfig
from core.io.telegram_approvals import TelegramApprovals
from core.io.telegram_bridge import TelegramBridge
from core.policy import ApprovalManager, TrustStore
from core.policy.risk import RiskClass


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


def _ok(payload) -> dict:
    return {"ok": True, "result": payload}


def _wire(chat_id: str = "999") -> tuple[Hub, ApprovalManager, TelegramApprovals]:
    settings = get_settings()
    ensure_schema(settings.db_path)
    hub = Hub()

    async def broadcast(event_type: str, payload: dict) -> None:
        await hub.broadcast(event_type, payload)

    trust = TrustStore()
    approvals = ApprovalManager(
        db_path=settings.db_path, trust_store=trust, broadcast=broadcast,
    )
    client = TelegramClient(
        TelegramConfig(bot_token="tok-abc", chat_id=chat_id),
    )
    bridge = TelegramApprovals(
        client=client, hub=hub, approvals=approvals, chat_id=chat_id,
    )
    bridge.start()
    return hub, approvals, bridge


# ── approval.created → sendMessage with buttons ──────────────────


@pytest.mark.asyncio
async def test_approval_created_emits_card_with_buttons() -> None:
    sends: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/sendMessage"):
            sends.append(_json.loads(req.content.decode()))
            return httpx.Response(
                200,
                json=_ok({"chat": {"id": 999}, "message_id": 55}),
            )
        raise AssertionError(f"unexpected url: {req.url}")

    _install_transport(handler)
    _, approvals, _ = _wire()

    req = await approvals.request(
        plan_id=None, step_id=None, agent_name="prospector",
        tool_name="net_fetch",
        args={"url": "https://example.com"},
        risk_class=RiskClass.NET_WRITE,
        reason="fetching a site outside the allowlist",
    )

    assert len(sends) == 1
    payload = sends[0]
    assert payload["chat_id"] == "999"
    # Agent name is rendered in human-readable Title Case ("Prospector"),
    # not the raw snake_case from the tool layer. Tool name is
    # humanized into the fallback sentence ("net fetch").
    assert "Prospector" in payload["text"]
    assert "net fetch" in payload["text"]
    assert "https://example.com" in payload["text"]
    markup = payload["reply_markup"]
    buttons = markup["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == f"approve:{req.id}"
    assert buttons[1]["callback_data"] == f"reject:{req.id}"


# ── callback_query: Approve button ───────────────────────────────


@pytest.mark.asyncio
async def test_approve_button_resolves_approval() -> None:
    """End-to-end: request → send card → tap Approve → gateway's
    Future resolves with decision='approved', card gets edited."""
    sent: list[dict] = []
    answered: list[dict] = []
    edited: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/sendMessage"):
            sent.append(_json.loads(req.content.decode()))
            return httpx.Response(
                200,
                json=_ok({"chat": {"id": 999}, "message_id": 77}),
            )
        if url.endswith("/answerCallbackQuery"):
            answered.append(_json.loads(req.content.decode()))
            return httpx.Response(200, json=_ok(True))
        if url.endswith("/editMessageText"):
            edited.append(_json.loads(req.content.decode()))
            return httpx.Response(
                200,
                json=_ok({"chat": {"id": 999}, "message_id": 77}),
            )
        raise AssertionError(f"unexpected url: {url}")

    _install_transport(handler)
    _, approvals, bridge = _wire()

    req = await approvals.request(
        plan_id=None, step_id=None, agent_name="a",
        tool_name="net_fetch", args={"url": "u"},
        risk_class=RiskClass.NET_WRITE, reason="r",
    )

    await bridge.handle_callback({
        "update_id": 1,
        "callback_query": {
            "id": "cbq-1",
            "data": f"approve:{req.id}",
            "message": {"chat": {"id": 999}, "message_id": 77},
        },
    })

    # Future resolved.
    assert req.future.done()
    decision = req.future.result()
    assert decision.decision == "approved"
    # Toast shown.
    assert answered[0]["callback_query_id"] == "cbq-1"
    assert "approved" in answered[0]["text"].lower()
    # Card rewritten with decision marker and empty keyboard.
    assert len(edited) == 1
    assert edited[0]["chat_id"] == "999"
    assert edited[0]["message_id"] == 77
    assert "Approved" in edited[0]["text"]
    assert edited[0]["reply_markup"]["inline_keyboard"] == []


# ── callback_query: Reject button ────────────────────────────────


@pytest.mark.asyncio
async def test_reject_button_resolves_approval() -> None:
    sent: list[dict] = []
    answered: list[dict] = []
    edited: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/sendMessage"):
            sent.append(_json.loads(req.content.decode()))
            return httpx.Response(
                200,
                json=_ok({"chat": {"id": 999}, "message_id": 77}),
            )
        if url.endswith("/answerCallbackQuery"):
            answered.append(_json.loads(req.content.decode()))
            return httpx.Response(200, json=_ok(True))
        if url.endswith("/editMessageText"):
            edited.append(_json.loads(req.content.decode()))
            return httpx.Response(200, json=_ok({}))
        raise AssertionError(f"unexpected url: {url}")

    _install_transport(handler)
    _, approvals, bridge = _wire()

    req = await approvals.request(
        plan_id=None, step_id=None, agent_name=None,
        tool_name="finance_deposit", args={"amount_usd": 50},
        risk_class=RiskClass.FINANCIAL, reason="manual deposit",
    )

    await bridge.handle_callback({
        "update_id": 2,
        "callback_query": {
            "id": "cbq-2",
            "data": f"reject:{req.id}",
            "message": {"chat": {"id": 999}, "message_id": 77},
        },
    })

    decision = req.future.result()
    assert decision.decision == "rejected"
    assert "rejected" in answered[0]["text"].lower()
    assert "Rejected" in edited[0]["text"]


# ── defensive paths ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_callback_from_foreign_chat_is_refused() -> None:
    """Callback_queries from any chat other than the configured
    chat_id never touch the approval queue — single-tenant by design
    matches the chat-bridge's posture."""
    answered: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/sendMessage"):
            return httpx.Response(
                200, json=_ok({"chat": {"id": 999}, "message_id": 1}),
            )
        if url.endswith("/answerCallbackQuery"):
            answered.append(_json.loads(req.content.decode()))
            return httpx.Response(200, json=_ok(True))
        raise AssertionError(f"unexpected url: {url}")

    _install_transport(handler)
    _, approvals, bridge = _wire()
    req = await approvals.request(
        plan_id=None, step_id=None, agent_name=None,
        tool_name="net_fetch", args={"url": "u"},
        risk_class=RiskClass.NET_WRITE, reason="r",
    )

    await bridge.handle_callback({
        "update_id": 3,
        "callback_query": {
            "id": "cbq-3",
            "data": f"approve:{req.id}",
            "message": {"chat": {"id": 12345}, "message_id": 1},
        },
    })

    # Approval stays pending, refusal toast shown.
    assert not req.future.done()
    assert any("not allowed" in a.get("text", "").lower() for a in answered)


@pytest.mark.asyncio
async def test_callback_malformed_data_is_rejected() -> None:
    answered: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/sendMessage"):
            return httpx.Response(
                200, json=_ok({"chat": {"id": 999}, "message_id": 1}),
            )
        if url.endswith("/answerCallbackQuery"):
            answered.append(_json.loads(req.content.decode()))
            return httpx.Response(200, json=_ok(True))
        raise AssertionError(f"unexpected url: {url}")

    _install_transport(handler)
    _, _, bridge = _wire()

    await bridge.handle_callback({
        "update_id": 4,
        "callback_query": {
            "id": "cbq-4",
            "data": "not-a-valid-payload",
            "message": {"chat": {"id": 999}, "message_id": 1},
        },
    })
    assert any("invalid" in a.get("text", "").lower() for a in answered)


@pytest.mark.asyncio
async def test_callback_already_resolved_is_idempotent() -> None:
    """If the operator resolves from the dashboard first and THEN taps
    the Telegram button, the second attempt must answer gracefully
    instead of 500-ing."""
    answered: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/sendMessage"):
            return httpx.Response(
                200, json=_ok({"chat": {"id": 999}, "message_id": 1}),
            )
        if url.endswith("/answerCallbackQuery"):
            answered.append(_json.loads(req.content.decode()))
            return httpx.Response(200, json=_ok(True))
        if url.endswith("/editMessageText"):
            return httpx.Response(200, json=_ok({}))
        raise AssertionError(f"unexpected url: {url}")

    _install_transport(handler)
    _, approvals, bridge = _wire()
    req = await approvals.request(
        plan_id=None, step_id=None, agent_name=None,
        tool_name="net_fetch", args={"url": "u"},
        risk_class=RiskClass.NET_WRITE, reason="r",
    )
    # Resolve via the REST path (or dashboard) first.
    await approvals.approve(req.id, reason="dashboard")

    # Now the operator taps the button. Should not raise.
    await bridge.handle_callback({
        "update_id": 5,
        "callback_query": {
            "id": "cbq-5",
            "data": f"approve:{req.id}",
            "message": {"chat": {"id": 999}, "message_id": 1},
        },
    })
    assert any(
        "already resolved" in a.get("text", "").lower()
        for a in answered
    )


# ── approval.resolved rewrites the card for dashboard-side decisions ──


@pytest.mark.asyncio
async def test_approval_resolved_edits_card_even_when_resolved_elsewhere() -> None:
    """Resolving from the dashboard should still update the Telegram
    card so the operator's chat history stays accurate."""
    edits: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/sendMessage"):
            return httpx.Response(
                200,
                json=_ok({"chat": {"id": 999}, "message_id": 42}),
            )
        if url.endswith("/editMessageText"):
            edits.append(_json.loads(req.content.decode()))
            return httpx.Response(200, json=_ok({}))
        raise AssertionError(f"unexpected url: {url}")

    _install_transport(handler)
    _, approvals, _ = _wire()
    req = await approvals.request(
        plan_id=None, step_id=None, agent_name=None,
        tool_name="net_fetch", args={"url": "u"},
        risk_class=RiskClass.NET_WRITE, reason="r",
    )
    await approvals.reject(req.id, reason="nope")
    assert len(edits) == 1
    assert edits[0]["message_id"] == 42
    assert "Rejected" in edits[0]["text"]


# ── TelegramBridge routes callback_query to the approvals bridge ─


@pytest.mark.asyncio
async def test_bridge_routes_callback_query_to_handler(
    tmp_path: Path,
) -> None:
    """The chat bridge + approvals bridge share one long-poll loop;
    this test locks in the contract that callback_query updates are
    handed straight to the callback_handler and never fall through
    to the chat dispatch path."""
    seen: list[dict] = []

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok({"message_id": 1}))

    _install_transport(handler)

    async def captured(update: dict) -> None:
        seen.append(update)

    class _Orch:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(self, goal: str, **kwargs: object) -> None:
            self.calls.append(goal)

    hub = Hub()
    orch = _Orch()
    bridge = TelegramBridge(
        config=TelegramConfig(bot_token="tok", chat_id="999"),
        orchestrator=orch,  # type: ignore[arg-type]
        hub=hub,
        state_path=tmp_path / "state" / "bridge.json",
        callback_handler=captured,
    )
    await bridge._handle_update({
        "update_id": 9,
        "callback_query": {
            "id": "x",
            "data": "approve:a_1",
            "message": {"chat": {"id": 999}, "message_id": 1},
        },
    })
    assert len(seen) == 1
    # Chat dispatch must NOT have fired.
    assert orch.calls == []


@pytest.mark.asyncio
async def test_bridge_allows_callback_updates_in_getupdates(
    tmp_path: Path,
) -> None:
    """Smoke-test that the bridge passes ``callback_query`` through to
    the ``allowed_updates`` query param — this is what tells Telegram
    to actually deliver button taps."""
    seen_allowed: list = []

    class _Orch:
        async def run(self, goal: str, **kwargs: object) -> None:
            del goal

    hub = Hub()
    bridge = TelegramBridge(
        config=TelegramConfig(bot_token="tok", chat_id="999"),
        orchestrator=_Orch(),  # type: ignore[arg-type]
        hub=hub,
        state_path=tmp_path / "bridge.json",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/getUpdates"):
            body = _json.loads(req.content.decode())
            seen_allowed.append(body.get("allowed_updates"))
            # Stop the bridge synchronously so the next iteration of
            # _run's while-loop exits instead of polling forever.
            bridge._stop.set()
            return httpx.Response(200, json=_ok([]))
        return httpx.Response(200, json=_ok({"message_id": 1}))

    _install_transport(handler)
    await bridge._run()

    assert seen_allowed, "getUpdates was never called"
    assert "message" in seen_allowed[0]
    assert "callback_query" in seen_allowed[0]


# ── _format_request: plain-English card ──────────────────────────


def test_format_request_reads_as_plain_english() -> None:
    """The specific case from the operator's screenshot: the card
    must read like someone texting them, not like a raw tool dump.
    No risk class jargon, no ISO timestamps, no tool name in the
    body, no contact IDs without context."""
    from core.io.telegram_approvals import _format_request

    out = _format_request({
        "agent_name": "lead_qualifier_agent",
        "tool_name": "ghl_task_create",
        "risk_class": "NET_WRITE",
        "reason": "NET_WRITE: requires approval",
        "args": {
            "contact_id": "location_id",
            "title": "Set ghl_default_location_id",
            "body": "Please set the default location ID in GHL settings.",
            "due_date": "2026-04-23T09:00:00",
        },
    })
    # Header phrasing.
    assert "PILK wants your approval" in out
    # Plain-English sentence, CRM instead of "GHL".
    assert "Create a task in your CRM" in out
    assert '"Set ghl_default_location_id"' in out
    # ISO timestamp is translated to a human phrase.
    assert "Thursday" in out
    assert "Apr 23" in out
    assert "9:00 AM" in out
    # Raw ISO string must NOT appear verbatim.
    assert "2026-04-23T09:00:00" not in out
    # Jargon that the user complained about MUST be gone.
    assert "NET_WRITE" not in out
    assert "ghl_task_create" not in out
    assert "Tool:" not in out
    assert "Risk:" not in out
    assert "requires approval" not in out
    # Agent attribution shows up as a human name, not snake_case.
    assert "Lead Qualifier" in out
    assert "lead_qualifier_agent" not in out


def test_format_request_gmail_send_is_plain_english() -> None:
    from core.io.telegram_approvals import _format_request

    out = _format_request({
        "agent_name": "inbox_triage_agent",
        "tool_name": "gmail_send_as_pilk",
        "risk_class": "COMMS",
        "reason": "",
        "args": {
            "to": "alice@example.com",
            "subject": "Quick question",
            "body": "Hey Alice, …",
        },
    })
    assert "Send an email to alice@example.com" in out
    assert '"Quick question"' in out
    assert "Inbox Triage" in out
    # No tool/risk metadata.
    assert "COMMS" not in out
    assert "gmail_send_as_pilk" not in out


def test_format_request_shell_spells_it_out() -> None:
    """Shell exec is one of the scariest approvals — the card needs
    to make obvious that PILK wants to run a command on the
    operator's machine."""
    from core.io.telegram_approvals import _format_request

    out = _format_request({
        "agent_name": "some_agent",
        "tool_name": "shell_exec",
        "risk_class": "EXEC_LOCAL",
        "reason": "",
        "args": {"command": "rm -rf /tmp/foo"},
    })
    assert "Run this command on your computer" in out
    assert "rm -rf /tmp/foo" in out


def test_format_request_unknown_tool_falls_back_gracefully() -> None:
    """Tools without a bespoke summary still render something the
    operator can read, even if not as polished."""
    from core.io.telegram_approvals import _format_request

    out = _format_request({
        "agent_name": "some_agent",
        "tool_name": "brand_new_tool_nobody_has_seen",
        "risk_class": "NET_WRITE",
        "reason": "",
        "args": {"payload": "hello world"},
    })
    assert "PILK wants your approval" in out
    # Tool name appears in the fallback sentence, humanized.
    assert "brand new tool nobody has seen" in out
    # Bare string, not repr('hello world').
    assert "hello world" in out
    assert "'hello world'" not in out
    # Arg label is capitalized English, not snake_case.
    assert "Payload:" in out


def test_format_request_non_boilerplate_reason_surfaces() -> None:
    """A meaningful agent-provided reason still shows up — we only
    filter out the policy-layer boilerplate that echoes the risk
    class."""
    from core.io.telegram_approvals import _format_request

    out = _format_request({
        "agent_name": "lead_qualifier_agent",
        "tool_name": "ghl_send_email",
        "risk_class": "COMMS",
        "reason": "Client hasn't replied in 10 days; nudging.",
        "args": {"contact_id": "abc123", "subject": "Following up"},
    })
    assert "Client hasn't replied in 10 days" in out
    assert "Why:" in out
