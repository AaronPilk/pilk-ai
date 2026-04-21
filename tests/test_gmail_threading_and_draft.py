"""Tests for Gmail ``reply_to_thread_id`` + the new ``gmail_draft_save_*``
tools.

Stubs the Google API at the ``_do_send`` / ``_do_draft_save`` seam
(same pattern as test_agent_email_deliver.py) so no OAuth client is
constructed. Verifies:

- Send attaches ``threadId`` on the API body only when
  ``reply_to_thread_id`` is passed (and NOT when it isn't).
- Draft-save creates a drafts.create body with the MIME payload and
  optional threadId.
- Both new surfaces carry the right risk class (COMMS vs WRITE_LOCAL).
- ``make_gmail_tools`` now emits 5 tools per role, including the
  draft-save tool.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.identity import AccountsStore
from core.identity.accounts import OAuthTokens
from core.integrations.google import make_gmail_tools
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext


@pytest.fixture
def accounts(tmp_path: Path) -> AccountsStore:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    return store


def _seed_user(accounts: AccountsStore, email: str = "me@x.com") -> None:
    accounts.upsert(
        provider="google",
        role="user",
        label="user",
        email=email,
        username=None,
        scopes=["gmail.send", "gmail.modify"],
        tokens=OAuthTokens(
            access_token="at",
            refresh_token="rt",
            client_id="cid",
            client_secret="cs",
            scopes=["gmail.send", "gmail.modify"],
        ),
        make_default=True,
    )


def _patch(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub the two sync Google helpers and the creds factory. Returns
    a dict keyed by call type ('send' / 'draft') so tests can inspect
    the body that would have been sent to the API."""
    from core.integrations.google import gmail as gmail_mod

    captured: dict = {"sends": [], "drafts": []}

    def fake_send(
        creds, raw_b64url: str, reply_to_thread_id: str | None = None,
    ) -> dict:
        captured["sends"].append(
            {
                "raw_b64url": raw_b64url,
                "reply_to_thread_id": reply_to_thread_id,
            }
        )
        return {"id": "m-abc", "threadId": reply_to_thread_id or "th-new"}

    def fake_draft(
        creds, raw_b64url: str, reply_to_thread_id: str | None = None,
    ) -> dict:
        captured["drafts"].append(
            {
                "raw_b64url": raw_b64url,
                "reply_to_thread_id": reply_to_thread_id,
            }
        )
        return {
            "id": "d-123",
            "message": {
                "id": "m-d",
                "threadId": reply_to_thread_id or "th-draft",
            },
        }

    monkeypatch.setattr(gmail_mod, "_do_send", fake_send)
    monkeypatch.setattr(gmail_mod, "_do_draft_save", fake_draft)

    class _FakeCreds:
        email = "me@x.com"

        def build(self, *a, **kw):  # pragma: no cover
            raise AssertionError("creds.build should never run in tests")

    monkeypatch.setattr(
        gmail_mod, "credentials_from_blob", lambda blob: _FakeCreds(),
    )
    return captured


def _get_tool(tools, name: str):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"no tool named {name}; have {[t.name for t in tools]}")


# ── tool surface shape ───────────────────────────────────────────


def test_user_role_emits_five_tools_including_draft(
    accounts: AccountsStore,
) -> None:
    tools = make_gmail_tools("user", accounts)
    names = [t.name for t in tools]
    assert names == [
        "gmail_send_as_me",
        "gmail_search_my_inbox",
        "gmail_read_me",
        "gmail_thread_read_me",
        "gmail_draft_save_as_me",
    ]


def test_system_role_emits_draft_save_variant(
    accounts: AccountsStore,
) -> None:
    tools = make_gmail_tools("system", accounts)
    names = [t.name for t in tools]
    assert "gmail_draft_save_as_pilk" in names


def test_draft_save_is_write_local_not_comms(
    accounts: AccountsStore,
) -> None:
    """Drafts don't leave the mailbox, so they don't belong in the
    approval queue's COMMS bucket. Locks in the risk class."""
    tools = make_gmail_tools("user", accounts)
    draft = _get_tool(tools, "gmail_draft_save_as_me")
    assert draft.risk == RiskClass.WRITE_LOCAL
    send = _get_tool(tools, "gmail_send_as_me")
    assert send.risk == RiskClass.COMMS


def test_send_schema_advertises_reply_to_thread_id(
    accounts: AccountsStore,
) -> None:
    """Catch the schema regression if a future refactor drops the
    new field — the planner would silently stop passing it."""
    tools = make_gmail_tools("user", accounts)
    send = _get_tool(tools, "gmail_send_as_me")
    props = send.input_schema["properties"]
    assert "reply_to_thread_id" in props


# ── send with / without reply_to_thread_id ───────────────────────


@pytest.mark.asyncio
async def test_send_without_thread_id_does_not_set_it(
    accounts: AccountsStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_user(accounts)
    captured = _patch(monkeypatch)
    tools = make_gmail_tools("user", accounts)
    send = _get_tool(tools, "gmail_send_as_me")
    out = await send.handler(
        {"to": "you@x.com", "subject": "hi", "body": "hey"},
        ToolContext(),
    )
    assert not out.is_error
    assert len(captured["sends"]) == 1
    assert captured["sends"][0]["reply_to_thread_id"] is None


@pytest.mark.asyncio
async def test_send_with_thread_id_is_passed_through(
    accounts: AccountsStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_user(accounts)
    captured = _patch(monkeypatch)
    tools = make_gmail_tools("user", accounts)
    send = _get_tool(tools, "gmail_send_as_me")
    out = await send.handler(
        {
            "to": "you@x.com",
            "subject": "Re: hi",
            "body": "ack",
            "reply_to_thread_id": "th-xyz-789",
        },
        ToolContext(),
    )
    assert not out.is_error
    assert captured["sends"][0]["reply_to_thread_id"] == "th-xyz-789"
    assert "existing thread" in out.content


# ── draft_save happy path + validation ───────────────────────────


@pytest.mark.asyncio
async def test_draft_save_happy_path(
    accounts: AccountsStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_user(accounts)
    captured = _patch(monkeypatch)
    tools = make_gmail_tools("user", accounts)
    draft = _get_tool(tools, "gmail_draft_save_as_me")
    out = await draft.handler(
        {
            "to": "you@x.com",
            "subject": "draft subject",
            "body": "work in progress",
        },
        ToolContext(),
    )
    assert not out.is_error
    assert captured["drafts"][0]["reply_to_thread_id"] is None
    assert out.data["draft_id"] == "d-123"
    assert out.data["to"] == "you@x.com"


@pytest.mark.asyncio
async def test_draft_save_with_thread_id(
    accounts: AccountsStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_user(accounts)
    captured = _patch(monkeypatch)
    tools = make_gmail_tools("user", accounts)
    draft = _get_tool(tools, "gmail_draft_save_as_me")
    await draft.handler(
        {
            "to": "you@x.com",
            "subject": "Re: ongoing",
            "body": "coming back to this",
            "reply_to_thread_id": "th-abc",
        },
        ToolContext(),
    )
    assert captured["drafts"][0]["reply_to_thread_id"] == "th-abc"


@pytest.mark.asyncio
async def test_draft_save_missing_to_is_error(
    accounts: AccountsStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_user(accounts)
    _patch(monkeypatch)
    tools = make_gmail_tools("user", accounts)
    draft = _get_tool(tools, "gmail_draft_save_as_me")
    out = await draft.handler(
        {"subject": "x", "body": "y"}, ToolContext(),
    )
    assert out.is_error
    assert "'to'" in out.content
