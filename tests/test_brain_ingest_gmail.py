"""Unit + tool-layer tests for the Gmail brain ingester.

We DO NOT hit the real Gmail API. A fake ``creds.build("gmail",
"v1")`` chain returns canned thread payloads shaped like the real
API so ``scan_threads`` / ``render_thread_note`` / the brain_ingest_
gmail tool can be exercised end-to-end without network.

The stub is deliberately pedantic about the exact method-call
sequence (``.users().threads().list/get(...).execute()``) because
one of the easiest ways to break this ingester is a schema drift on
the Gmail client — if that happens we want the test to fail on a
missing attribute, not a subtle wrong-shape assertion later.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from core.brain import Vault
from core.integrations.ingesters.gmail import (
    DEFAULT_MAX_THREADS,
    DEFAULT_QUERY,
    GmailMessage,
    GmailThread,
    render_thread_note,
    scan_threads,
)
from core.tools.builtin.brain_ingest import make_brain_ingest_tools
from core.tools.registry import ToolContext

# ── Gmail API stub ──────────────────────────────────────────────


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _message(
    *,
    mid: str,
    subject: str,
    sender: str,
    to: str,
    date: str,
    body: str,
    mime: str = "text/plain",
) -> dict:
    return {
        "id": mid,
        "snippet": body[:80],
        "payload": {
            "mimeType": mime,
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "To", "value": to},
                {"name": "Date", "value": date},
            ],
            "body": {"data": _b64url(body)},
        },
    }


class _Exec:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def execute(self) -> object:
        return self._payload


class _Threads:
    def __init__(self, by_id: dict[str, dict], listing: dict) -> None:
        self._by_id = by_id
        self._listing = listing
        self.calls: list[dict] = []

    def list(self, **kw):
        self.calls.append({"op": "list", **kw})
        return _Exec(self._listing)

    def get(self, **kw):
        self.calls.append({"op": "get", **kw})
        thread = self._by_id.get(kw["id"])
        return _Exec(thread)


class _Users:
    def __init__(self, threads: _Threads) -> None:
        self._threads = threads

    def threads(self):
        return self._threads


class _Service:
    def __init__(self, threads: _Threads) -> None:
        self._threads = threads

    def users(self):
        return _Users(self._threads)


class _StubCreds:
    """Stand-in for the credentials object. The real one exposes
    ``build(api, version)``; we mirror that."""

    def __init__(self, threads: _Threads) -> None:
        self._threads = threads

    def build(self, api: str, version: str):
        assert api == "gmail", f"unexpected api: {api}"
        assert version == "v1", f"unexpected version: {version}"
        return _Service(self._threads)


def _stub_creds_from_threads(
    threads_by_id: dict[str, dict],
    *,
    listing_ids: list[str] | None = None,
) -> _StubCreds:
    ids = listing_ids if listing_ids is not None else list(threads_by_id)
    listing = {"threads": [{"id": tid} for tid in ids]}
    return _StubCreds(_Threads(threads_by_id, listing))


# ── scan_threads ────────────────────────────────────────────────


def test_scan_threads_flattens_list_get_cycle() -> None:
    """One ``list`` call, one ``get`` per id, newest-first output."""
    threads = {
        "old": {
            "messages": [
                _message(
                    mid="m1", subject="Old thread", sender="a@x.co",
                    to="me@x.co",
                    date="Mon, 01 Feb 2026 10:00:00 +0000",
                    body="original body",
                ),
            ],
        },
        "new": {
            "messages": [
                _message(
                    mid="m2", subject="Newer thread", sender="b@x.co",
                    to="me@x.co",
                    date="Fri, 05 Feb 2026 10:00:00 +0000",
                    body="fresher",
                ),
            ],
        },
    }
    creds = _stub_creds_from_threads(threads, listing_ids=["old", "new"])
    result = scan_threads(creds, query="newer_than:30d", max_threads=25)
    # Two threads returned, sorted newest-first by message date.
    assert [t.thread_id for t in result] == ["new", "old"]
    assert result[0].subject == "Newer thread"
    assert result[0].messages[0].body == "fresher"


def test_scan_threads_query_and_limit_propagate() -> None:
    """Query + max_results land on the underlying list() call so
    Gmail filters at its end instead of dumping the whole inbox."""
    creds = _stub_creds_from_threads({"a": {"messages": []}})
    scan_threads(creds, query="is:starred", max_threads=3)
    list_call = next(
        c for c in creds._threads.calls if c["op"] == "list"
    )
    assert list_call["q"] == "is:starred"
    assert list_call["maxResults"] == 3


def test_scan_threads_drops_empty_threads() -> None:
    """A thread with no messages must be skipped, not surfaced as an
    empty note."""
    creds = _stub_creds_from_threads(
        {"blank": {"messages": []}, "real": {
            "messages": [
                _message(
                    mid="x", subject="Real one", sender="a@x.co",
                    to="me@x.co",
                    date="Mon, 01 Feb 2026 10:00:00 +0000",
                    body="hi",
                ),
            ],
        }},
        listing_ids=["blank", "real"],
    )
    result = scan_threads(creds)
    assert [t.thread_id for t in result] == ["real"]


def test_scan_threads_swallows_single_thread_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ONE threads.get blows up, the rest of the ingest should
    carry on — we don't abort a 100-thread bulk because one id
    404's."""
    threads = {
        "ok": {
            "messages": [
                _message(
                    mid="m1", subject="Ok", sender="a@x.co",
                    to="me@x.co",
                    date="Mon, 01 Feb 2026 10:00:00 +0000",
                    body="hi",
                ),
            ],
        },
        "bad": {"messages": []},
    }
    creds = _stub_creds_from_threads(threads, listing_ids=["ok", "bad"])

    original_get = creds._threads.get

    def failing_get(**kw):
        if kw["id"] == "bad":
            raise RuntimeError("simulated 500")
        return original_get(**kw)

    creds._threads.get = failing_get  # type: ignore[method-assign]
    result = scan_threads(creds)
    assert [t.thread_id for t in result] == ["ok"]


def test_scan_threads_html_fallback() -> None:
    """HTML-only messages (no text/plain) land as HTML-stripped
    text so the vault still gets readable content."""
    html_body = "<p>Hello <b>world</b></p>"
    multipart = {
        "messages": [
            {
                "id": "h1",
                "snippet": "Hello",
                "payload": {
                    "mimeType": "multipart/alternative",
                    "headers": [
                        {"name": "Subject", "value": "HTML only"},
                        {"name": "From", "value": "h@x.co"},
                        {"name": "To", "value": "me@x.co"},
                        {
                            "name": "Date",
                            "value": "Mon, 01 Feb 2026 10:00:00 +0000",
                        },
                    ],
                    "parts": [
                        {
                            "mimeType": "text/html",
                            "body": {"data": _b64url(html_body)},
                        },
                    ],
                },
            },
        ],
    }
    creds = _stub_creds_from_threads({"h": multipart})
    result = scan_threads(creds)
    # Tags stripped; content preserved.
    assert "Hello" in result[0].messages[0].body
    assert "<b>" not in result[0].messages[0].body


def test_scan_threads_defaults_match_module_constants() -> None:
    """Cheap sentinel test: if anyone changes the default query /
    limit, they change it deliberately. Reaching for 'is:unread'
    as a default would silently skip read mail."""
    assert DEFAULT_QUERY.startswith("newer_than:")
    assert DEFAULT_MAX_THREADS >= 50


# ── render_thread_note ─────────────────────────────────────────


def test_render_thread_note_path_and_metadata() -> None:
    thread = GmailThread(
        thread_id="abc-123",
        subject="Client: Skyway Q2 kickoff",
        messages=[
            GmailMessage(
                message_id="m1",
                from_="alice@skyway.media",
                to="aaron@skyway.media",
                subject="Client: Skyway Q2 kickoff",
                date=None,
                body="first pass",
            ),
        ],
    )
    note = render_thread_note(thread)
    # Path starts with a date prefix + uses sanitised stem; no colons
    # or slashes leak into the filename.
    assert note.path.startswith("ingested/gmail/")
    assert ":" not in note.path
    assert "skyway" in note.path.lower()
    # Metadata surface: subject + thread id + message count.
    assert "Client: Skyway Q2 kickoff" in note.body
    assert "abc-123" in note.body
    assert "Messages: 1" in note.body


def test_render_thread_note_dedupes_participants() -> None:
    thread = GmailThread(
        thread_id="t1",
        subject="Group chat",
        messages=[
            GmailMessage(
                message_id="a", from_="a@x.co", to="b@x.co",
                subject="Group chat", date=None, body="hi",
            ),
            GmailMessage(
                message_id="b", from_="b@x.co", to="a@x.co",
                subject="Re: Group chat", date=None, body="hi back",
            ),
        ],
    )
    note = render_thread_note(thread)
    # Participants are deduplicated + ordered by first-seen.
    part_line = next(
        ln for ln in note.body.splitlines()
        if ln.startswith("- Participants:")
    )
    assert part_line.count("a@x.co") == 1
    assert part_line.count("b@x.co") == 1


# ── tool layer ─────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    v = Vault(tmp_path / "brain")
    v.ensure_initialized()
    return v


class _FakeAccount:
    def __init__(self, email: str = "you@x.co") -> None:
        self.email = email
        self.account_id = "acct-1"


class _FakeTokens:
    access_token = "at"
    refresh_token = "rt"
    client_id = "cid"
    client_secret = "cs"
    scopes = ("https://www.googleapis.com/auth/gmail.readonly",)
    token_uri = "https://oauth2.googleapis.com/token"


class _FakeAccounts:
    """Minimal AccountsStore shape: resolve_binding + load_tokens."""

    def __init__(
        self, *, has_account: bool = True, has_tokens: bool = True,
    ) -> None:
        self.has_account = has_account
        self.has_tokens = has_tokens

    def resolve_binding(self, binding):  # pragma: no cover — trivial
        return _FakeAccount() if self.has_account else None

    def load_tokens(self, account_id):
        return _FakeTokens() if self.has_tokens else None


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(sandbox_root=None)


@pytest.mark.asyncio
async def test_tool_without_accounts_store_fails_clean(
    vault: Vault, ctx: ToolContext,
) -> None:
    """The factory supports accounts=None; the Gmail tool must then
    return a clean error rather than crashing on attribute access."""
    tools = make_brain_ingest_tools(vault, accounts=None)
    gmail_tool = next(t for t in tools if t.name == "brain_ingest_gmail")
    out = await gmail_tool.handler({}, ctx)
    assert out.is_error
    assert "account store" in out.content or "identity wiring" in out.content


@pytest.mark.asyncio
async def test_tool_without_linked_account_points_at_settings(
    vault: Vault, ctx: ToolContext,
) -> None:
    accounts = _FakeAccounts(has_account=False)
    tools = make_brain_ingest_tools(vault, accounts=accounts)
    gmail_tool = next(t for t in tools if t.name == "brain_ingest_gmail")
    out = await gmail_tool.handler({}, ctx)
    assert out.is_error
    # The operator should get a pointer to the fix, not a stack trace.
    assert "Settings" in out.content or "link" in out.content.lower()


@pytest.mark.asyncio
async def test_tool_happy_path_writes_to_vault(
    vault: Vault, ctx: ToolContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end with a stubbed Google client: tool returns success
    and a note lands in the vault."""
    threads = {
        "t1": {
            "messages": [
                _message(
                    mid="m1", subject="Q2 plan",
                    sender="client@acme.co", to="you@x.co",
                    date="Mon, 01 Feb 2026 10:00:00 +0000",
                    body="Here's the brief.",
                ),
            ],
        },
    }
    creds = _stub_creds_from_threads(threads)

    # Short-circuit the OAuth loader inside the tool.
    import core.tools.builtin.brain_ingest as mod
    monkeypatch.setattr(
        mod, "_load_user_gmail_creds",
        lambda _accounts: (creds, _FakeAccount()),
    )

    accounts = _FakeAccounts()
    tools = make_brain_ingest_tools(vault, accounts=accounts)
    gmail_tool = next(t for t in tools if t.name == "brain_ingest_gmail")
    out = await gmail_tool.handler({}, ctx)
    assert not out.is_error
    assert out.data["threads_scanned"] == 1
    written = out.data["written"][0]
    assert written["path"].startswith("ingested/gmail/")
    assert (vault.root / written["path"]).is_file()
    # The saved note actually contains the body + subject.
    content = (vault.root / written["path"]).read_text()
    assert "Q2 plan" in content
    assert "Here's the brief." in content


@pytest.mark.asyncio
async def test_tool_empty_result_returns_helpful_message(
    vault: Vault, ctx: ToolContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds = _stub_creds_from_threads({}, listing_ids=[])
    import core.tools.builtin.brain_ingest as mod
    monkeypatch.setattr(
        mod, "_load_user_gmail_creds",
        lambda _accounts: (creds, _FakeAccount()),
    )
    accounts = _FakeAccounts()
    tools = make_brain_ingest_tools(vault, accounts=accounts)
    gmail_tool = next(t for t in tools if t.name == "brain_ingest_gmail")
    out = await gmail_tool.handler({"query": "is:important"}, ctx)
    # Not an error — just "zero hits, try a wider query."
    assert not out.is_error
    assert "zero threads" in out.content or "0" in out.content
