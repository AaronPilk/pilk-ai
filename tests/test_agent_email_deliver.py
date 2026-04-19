"""agent_email_deliver tool tests.

Covers subject formatting, attachment loading, link appending, allowlist
predicate, and trust-bypass behavior when the predicate is wired into
a TrustStore. Gmail API send is stubbed at the ``_do_send`` seam so
tests don't construct an OAuth client.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.identity import AccountsStore
from core.identity.accounts import OAuthTokens
from core.policy.risk import RiskClass
from core.policy.trust import TrustStore
from core.tools.builtin.delivery.email import (
    SUBJECT_FORMAT,
    make_agent_email_deliver_tool,
    recipients_in_allowlist,
)
from core.tools.registry import ToolContext


@pytest.fixture
def accounts(tmp_path: Path) -> AccountsStore:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    return store


def _seed_system_account(accounts: AccountsStore, email: str = "sys@x.com") -> None:
    accounts.upsert(
        provider="google",
        role="system",
        label="outbound",
        email=email,
        username=None,
        scopes=["gmail.send"],
        tokens=OAuthTokens(
            access_token="at",
            refresh_token="rt",
            client_id="cid",
            client_secret="cs",
            scopes=["gmail.send"],
        ),
        make_default=True,
    )


# ── Subject + argument validation ───────────────────────────────


def test_subject_format_constant() -> None:
    """Format string contract — every agent's subject matches this
    shape. Changing the format is a migration, not a patch."""
    assert SUBJECT_FORMAT == "[{agent_name}] {task_description}"


@pytest.mark.asyncio
async def test_missing_to_list(accounts: AccountsStore) -> None:
    tool = make_agent_email_deliver_tool(accounts)
    out = await tool.handler(
        {"task_description": "t", "body": "b"},
        ToolContext(agent_name="test_agent"),
    )
    assert out.is_error
    assert "'to'" in out.content


@pytest.mark.asyncio
async def test_string_to_is_coerced_to_list(
    accounts: AccountsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_system_account(accounts)
    _patch_send(monkeypatch, {"id": "m-1", "threadId": "t-1"})
    tool = make_agent_email_deliver_tool(accounts)
    out = await tool.handler(
        {
            "to": "one@example.com",
            "task_description": "t",
            "body": "b",
        },
        ToolContext(agent_name="test_agent"),
    )
    assert not out.is_error
    assert out.data["to"] == ["one@example.com"]


@pytest.mark.asyncio
async def test_invalid_email_refused(accounts: AccountsStore) -> None:
    tool = make_agent_email_deliver_tool(accounts)
    out = await tool.handler(
        {
            "to": ["not-an-email"],
            "task_description": "t",
            "body": "b",
        },
        ToolContext(agent_name="test_agent"),
    )
    assert out.is_error
    assert "email" in out.content


@pytest.mark.asyncio
async def test_agent_name_inferred_from_context(
    accounts: AccountsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_system_account(accounts)
    captured = _patch_send(monkeypatch, {"id": "m-1", "threadId": "t-1"})
    tool = make_agent_email_deliver_tool(accounts)
    out = await tool.handler(
        {
            "to": ["x@y.com"],
            "task_description": "Q1 report",
            "body": "b",
        },
        ToolContext(agent_name="sales_ops_agent"),
    )
    assert not out.is_error
    assert out.data["subject"] == "[sales_ops_agent] Q1 report"
    # Verify the send body carried the expected subject.
    raw = captured["raw"]
    assert "[sales_ops_agent] Q1 report" in raw


@pytest.mark.asyncio
async def test_empty_body_refused(accounts: AccountsStore) -> None:
    tool = make_agent_email_deliver_tool(accounts)
    out = await tool.handler(
        {
            "to": ["x@y.com"],
            "agent_name": "a",
            "task_description": "t",
            "body": "   ",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "body" in out.content


@pytest.mark.asyncio
async def test_attachments_must_be_list(accounts: AccountsStore) -> None:
    tool = make_agent_email_deliver_tool(accounts)
    out = await tool.handler(
        {
            "to": ["x@y.com"],
            "agent_name": "a",
            "task_description": "t",
            "body": "b",
            "attachments": "not a list",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "attachments" in out.content


@pytest.mark.asyncio
async def test_no_account_linked(accounts: AccountsStore) -> None:
    tool = make_agent_email_deliver_tool(accounts)
    out = await tool.handler(
        {
            "to": ["x@y.com"],
            "agent_name": "a",
            "task_description": "t",
            "body": "b",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "system" in out.content
    assert "Connected accounts" in out.content


# ── Send path ─────────────────────────────────────────────────


def _patch_send(
    monkeypatch: pytest.MonkeyPatch, response: dict
) -> dict:
    """Stub ``_do_send``. Returns a dict the caller can inspect after
    invocation to verify the raw MIME payload we handed to Gmail."""
    captured: dict = {}
    from core.tools.builtin.delivery import email as email_mod

    def fake_send(creds, raw_b64url: str) -> dict:
        import base64

        captured["raw"] = base64.urlsafe_b64decode(raw_b64url).decode("utf-8")
        return response

    monkeypatch.setattr(email_mod, "_do_send", fake_send)

    # Also stub credentials_from_blob so we don't instantiate real Google creds.
    class _FakeCreds:
        email = "sys@x.com"

        def build(self, *a, **kw):
            raise AssertionError("should not be built in tests — _do_send is stubbed")

    monkeypatch.setattr(
        email_mod,
        "credentials_from_blob",
        lambda blob: _FakeCreds(),
    )
    return captured


@pytest.mark.asyncio
async def test_send_happy_path(
    accounts: AccountsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_system_account(accounts, email="sys@x.com")
    captured = _patch_send(
        monkeypatch, {"id": "m-abc", "threadId": "th-xyz"}
    )
    tool = make_agent_email_deliver_tool(accounts)
    out = await tool.handler(
        {
            "to": ["aaron@skyway.media", "pilkingtonent@gmail.com"],
            "agent_name": "pitch_deck_agent",
            "task_description": "Q4 investor deck",
            "body": "See attached slides.",
            "links": ["https://docs.google.com/presentation/d/abc"],
        },
        ToolContext(),
    )
    assert not out.is_error
    assert out.data["message_id"] == "m-abc"
    assert out.data["subject"] == "[pitch_deck_agent] Q4 investor deck"
    assert out.data["attachment_count"] == 0
    assert out.data["link_count"] == 1
    # Subject line landed.
    assert "[pitch_deck_agent] Q4 investor deck" in captured["raw"]
    # Links section appended.
    assert "Links:" in captured["raw"]
    assert "https://docs.google.com/presentation/d/abc" in captured["raw"]
    # Multiple recipients joined.
    assert "aaron@skyway.media" in captured["raw"]
    assert "pilkingtonent@gmail.com" in captured["raw"]


@pytest.mark.asyncio
async def test_send_with_attachment(
    accounts: AccountsStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _seed_system_account(accounts)
    captured = _patch_send(monkeypatch, {"id": "m-1", "threadId": "t-1"})
    attachment = tmp_path / "report.txt"
    attachment.write_text("quarterly numbers")

    tool = make_agent_email_deliver_tool(accounts)
    out = await tool.handler(
        {
            "to": ["ops@x.com"],
            "agent_name": "sales_ops_agent",
            "task_description": "Weekly report",
            "body": "Attached.",
            "attachments": [str(attachment)],
        },
        ToolContext(),
    )
    assert not out.is_error
    assert out.data["attachment_count"] == 1
    # Filename appears in the MIME body.
    assert "report.txt" in captured["raw"]


@pytest.mark.asyncio
async def test_send_missing_attachment_errors(
    accounts: AccountsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_system_account(accounts)
    _patch_send(monkeypatch, {"id": "m", "threadId": "t"})
    tool = make_agent_email_deliver_tool(accounts)
    out = await tool.handler(
        {
            "to": ["ops@x.com"],
            "agent_name": "a",
            "task_description": "t",
            "body": "b",
            "attachments": ["/definitely/not/a/real/path.pdf"],
        },
        ToolContext(),
    )
    assert out.is_error
    assert "attachment not found" in out.content


def test_risk_class_is_net_write(accounts: AccountsStore) -> None:
    tool = make_agent_email_deliver_tool(accounts)
    assert tool.risk == RiskClass.NET_WRITE


# ── recipients_in_allowlist predicate ──────────────────────────


def test_allowlist_predicate_true_for_single_match() -> None:
    pred = recipients_in_allowlist({"a@x.com", "b@x.com"})
    assert pred({"to": ["a@x.com"]}) is True


def test_allowlist_predicate_true_for_all_match() -> None:
    pred = recipients_in_allowlist({"a@x.com", "b@x.com"})
    assert pred({"to": ["a@x.com", "b@x.com"]}) is True


def test_allowlist_predicate_false_when_one_outside() -> None:
    pred = recipients_in_allowlist({"a@x.com"})
    assert pred({"to": ["a@x.com", "stranger@y.com"]}) is False


def test_allowlist_predicate_false_on_empty_to() -> None:
    pred = recipients_in_allowlist({"a@x.com"})
    assert pred({}) is False
    assert pred({"to": []}) is False


def test_allowlist_predicate_accepts_scalar_to() -> None:
    pred = recipients_in_allowlist({"a@x.com"})
    assert pred({"to": "a@x.com"}) is True


def test_allowlist_predicate_is_case_insensitive() -> None:
    pred = recipients_in_allowlist({"a@x.com"})
    assert pred({"to": ["A@X.COM"]}) is True


# ── Integration: TrustStore + predicate ────────────────────────


def test_trust_rule_bypasses_for_allowlist() -> None:
    store = TrustStore()
    store.add(
        agent_name=None,
        tool_name="agent_email_deliver",
        permanent=True,
        predicate=recipients_in_allowlist(
            {"aaron@skyway.media", "pilkingtonent@gmail.com"}
        ),
        predicate_label="internal allowlist",
    )
    # Aaron only → trusted.
    assert (
        store.match(
            agent_name=None,
            tool_name="agent_email_deliver",
            args={"to": ["aaron@skyway.media"]},
        )
        is not None
    )
    # Both → trusted.
    assert (
        store.match(
            agent_name=None,
            tool_name="agent_email_deliver",
            args={
                "to": ["aaron@skyway.media", "pilkingtonent@gmail.com"]
            },
        )
        is not None
    )
    # External recipient mixed in → NOT trusted.
    assert (
        store.match(
            agent_name=None,
            tool_name="agent_email_deliver",
            args={
                "to": ["aaron@skyway.media", "stranger@example.com"]
            },
        )
        is None
    )


def test_trust_rule_narrower_than_tool_name_alone() -> None:
    """A bare tool-name rule without the predicate would approve ANY
    recipient. Verify the predicate is actually enforced — regression
    guard against someone removing the predicate wiring by accident."""
    store = TrustStore()
    store.add(
        agent_name=None,
        tool_name="agent_email_deliver",
        permanent=True,
        predicate=recipients_in_allowlist({"aaron@skyway.media"}),
    )
    # With no predicate, this would match. With the predicate, it
    # must NOT match.
    assert (
        store.match(
            agent_name=None,
            tool_name="agent_email_deliver",
            args={"to": ["anyone-else@example.com"]},
        )
        is None
    )
