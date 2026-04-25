"""Tests for ``make_gmail_draft_best_of_n_tool`` — cross-model BoN
drafting with GPT-5.5 variants and a Haiku judge.

No real network: the OpenAI HTTP call and the Gmail save are both
overridden via the factory's ``openai_caller`` / ``save_draft_caller``
seams, and the Anthropic client is the same minimal fake used in the
memory-distill tests. The vault is a real ``Vault`` rooted at
``tmp_path`` so we can assert the telemetry note actually lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from core.brain.vault import Vault
from core.identity import AccountsStore
from core.identity.accounts import OAuthTokens
from core.tools.builtin.delivery.gmail_draft_best_of_n import (
    make_gmail_draft_best_of_n_tool,
)
from core.tools.registry import ToolContext

# ── Fakes ───────────────────────────────────────────────────────


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


class _FakeAnthropic:
    """Anthropic client whose .messages.create returns a queued reply.
    Raises if no replies are left so test failure modes are loud."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        outer = self

        class _Messages:
            async def create(self, **kwargs: Any) -> Any:
                outer.calls.append(kwargs)
                if not outer._replies:
                    raise RuntimeError("no anthropic replies queued")
                text = outer._replies.pop(0)

                class _Resp:
                    def __init__(self) -> None:
                        self.content = [_TextBlock(text=text)]

                return _Resp()

        self.messages = _Messages()


class _RaisingAnthropic:
    """Like _FakeAnthropic but every call raises — exercises the
    judge-failure fallback."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        outer = self

        class _Messages:
            async def create(self, **kwargs: Any) -> Any:
                outer.calls.append(kwargs)
                raise RuntimeError("haiku rate limited")

        self.messages = _Messages()


class _FakeCreds:
    """Stand-in for Google credentials. Only ``email`` is read by the
    tool; everything else lives behind the save_draft_caller seam."""

    email = "operator@example.com"


# ── Fixtures + helpers ──────────────────────────────────────────


@pytest.fixture
def accounts(tmp_path: Path) -> AccountsStore:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    return store


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    v = Vault(tmp_path / "PILK-brain")
    v.ensure_initialized()
    return v


def _seed_user(accounts: AccountsStore) -> None:
    accounts.upsert(
        provider="google",
        role="user",
        label="user",
        email="operator@example.com",
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


def _patch_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the creds-from-blob factory so we don't need real OAuth."""
    from core.tools.builtin.delivery import gmail_draft_best_of_n as mod

    monkeypatch.setattr(
        mod, "credentials_from_blob", lambda blob: _FakeCreds()
    )


def _make_tool(
    *,
    accounts: AccountsStore,
    vault: Vault,
    anthropic_replies: list[str] | None = None,
    anthropic_raises: bool = False,
    openai_caller: Any = None,
    save_caller: Any = None,
    openai_api_key: str | None = "sk-test",
):
    if anthropic_raises:
        client: Any = _RaisingAnthropic()
    else:
        client = _FakeAnthropic(anthropic_replies or [])
    tool = make_gmail_draft_best_of_n_tool(
        role="user",
        accounts=accounts,
        anthropic_client=client,
        openai_api_key=openai_api_key,
        vault=vault,
        openai_caller=openai_caller,
        save_draft_caller=save_caller,
    )
    return tool, client


def _judge_reply(rankings: list[dict[str, Any]]) -> str:
    import json

    return json.dumps({"rankings": rankings})


def _gpt_drafts_reply(drafts: list[str]) -> str:
    import json

    return json.dumps({"drafts": drafts})


# ── Tool surface ────────────────────────────────────────────────


def test_tool_surface_basic(
    accounts: AccountsStore, vault: Vault,
) -> None:
    tool, _ = _make_tool(accounts=accounts, vault=vault)
    assert tool.name == "gmail_draft_best_of_n_as_me"
    props = tool.input_schema["properties"]
    assert set(tool.input_schema["required"]) == {
        "to", "subject", "brief", "opus_candidates",
    }
    assert props["opus_candidates"]["maxItems"] == 3


def test_tool_surface_pilk_role(
    accounts: AccountsStore, vault: Vault,
) -> None:
    client = _FakeAnthropic([])
    tool = make_gmail_draft_best_of_n_tool(
        role="system",
        accounts=accounts,
        anthropic_client=client,
        openai_api_key=None,
        vault=vault,
    )
    assert tool.name == "gmail_draft_best_of_n_as_pilk"


# ── Validation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_to_returns_error(
    accounts: AccountsStore, vault: Vault,
) -> None:
    tool, _ = _make_tool(accounts=accounts, vault=vault)
    out = await tool.handler(
        {
            "subject": "hi",
            "brief": "say hi",
            "opus_candidates": ["hello"],
        },
        ToolContext(),
    )
    assert out.is_error
    assert "'to'" in out.content


@pytest.mark.asyncio
async def test_missing_brief_returns_error(
    accounts: AccountsStore, vault: Vault,
) -> None:
    tool, _ = _make_tool(accounts=accounts, vault=vault)
    out = await tool.handler(
        {
            "to": "you@x.com",
            "subject": "hi",
            "opus_candidates": ["hello"],
        },
        ToolContext(),
    )
    assert out.is_error
    assert "brief" in out.content


@pytest.mark.asyncio
async def test_empty_opus_candidates_returns_error(
    accounts: AccountsStore, vault: Vault,
) -> None:
    tool, _ = _make_tool(accounts=accounts, vault=vault)
    out = await tool.handler(
        {
            "to": "you@x.com",
            "subject": "hi",
            "brief": "say hi",
            "opus_candidates": [],
        },
        ToolContext(),
    )
    assert out.is_error
    assert "non-empty" in out.content or "1+" in out.content


@pytest.mark.asyncio
async def test_no_linked_account_returns_error(
    accounts: AccountsStore, vault: Vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No account seeded — _load_creds returns (None, None).
    tool, _ = _make_tool(accounts=accounts, vault=vault)
    out = await tool.handler(
        {
            "to": "you@x.com",
            "subject": "hi",
            "brief": "say hi",
            "opus_candidates": ["hello"],
        },
        ToolContext(),
    )
    assert out.is_error
    assert "Settings" in out.content


# ── Happy path: cross-model best-of-N ───────────────────────────


@pytest.mark.asyncio
async def test_happy_path_picks_winner_and_saves(
    accounts: AccountsStore, vault: Vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: 2 Opus candidates + 2 GPT-5.5 variants → 4 total →
    judge ranks → winner saved → telemetry note written."""
    _seed_user(accounts)
    _patch_creds(monkeypatch)

    saves: list[dict[str, Any]] = []

    async def fake_save(creds, raw_b64url, thread_id):
        saves.append(
            {
                "raw": raw_b64url,
                "thread_id": thread_id,
                "creds_email": getattr(creds, "email", None),
            }
        )
        return {"id": "draft-abc123def456", "message": {"id": "m-1"}}

    openai_calls: list[dict[str, Any]] = []

    async def fake_openai(api_key, model, system, user_message):
        openai_calls.append(
            {
                "api_key": api_key,
                "model": model,
                "user": user_message,
            }
        )
        return _gpt_drafts_reply(["GPT draft A", "GPT draft B"])

    judge = _judge_reply(
        [
            {
                "index": 3,
                "hook": 0.9, "clarity": 0.9, "tone_fit": 0.9,
                "cta_strength": 0.9, "verdict": "keep",
                "reason": "strongest hook + clear CTA",
            },
            {
                "index": 0,
                "hook": 0.6, "clarity": 0.6, "tone_fit": 0.6,
                "cta_strength": 0.6, "verdict": "keep",
                "reason": "fine",
            },
            {
                "index": 1,
                "hook": 0.4, "clarity": 0.4, "tone_fit": 0.4,
                "cta_strength": 0.4, "verdict": "drop",
                "reason": "weaker",
            },
            {
                "index": 2,
                "hook": 0.5, "clarity": 0.5, "tone_fit": 0.5,
                "cta_strength": 0.5, "verdict": "drop",
                "reason": "weaker",
            },
        ]
    )

    tool, client = _make_tool(
        accounts=accounts,
        vault=vault,
        anthropic_replies=[judge],
        openai_caller=fake_openai,
        save_caller=fake_save,
    )
    out = await tool.handler(
        {
            "to": "lead@example.com",
            "subject": "Quick follow-up",
            "brief": "warm follow-up after demo, propose a 15-min call",
            "opus_candidates": ["Opus draft A", "Opus draft B"],
        },
        ToolContext(),
    )
    assert not out.is_error, out.content
    data = out.data
    assert data["draft_id"] == "draft-abc123def456"
    assert data["winner_index"] == 3       # 4th candidate (GPT draft B)
    assert data["winner_source"] == "gpt-5.5"
    assert data["winner_body"] == "GPT draft B"
    assert data["candidates_evaluated"] == 4
    assert data["opus_count"] == 2
    assert data["gpt_count"] == 2
    assert data["score"] == pytest.approx(0.9)
    # Both LLM hops happened.
    assert len(openai_calls) == 1
    assert openai_calls[0]["model"] == "gpt-5.5"
    assert len(client.calls) == 1
    # The Gmail save fired exactly once with the winner body.
    assert len(saves) == 1
    # Telemetry note exists in the vault.
    assert data["telemetry_log"]
    note_path = vault.root / data["telemetry_log"]
    assert note_path.exists()
    body = note_path.read_text()
    assert "gpt-5.5" in body
    assert "Opus draft A" in body
    assert "GPT draft B" in body


# ── Failure-mode fallbacks ──────────────────────────────────────


@pytest.mark.asyncio
async def test_gpt_failure_ranks_only_opus(
    accounts: AccountsStore, vault: Vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If GPT-5.5 errors out, the judge ranks the Opus candidates alone
    and we still save a winner."""
    _seed_user(accounts)
    _patch_creds(monkeypatch)

    async def boom_openai(*args, **kwargs):
        raise RuntimeError("openai 503")

    async def fake_save(creds, raw_b64url, thread_id):
        return {"id": "draft-xyz789", "message": {}}

    judge = _judge_reply(
        [
            {
                "index": 1, "hook": 0.8, "clarity": 0.8, "tone_fit": 0.8,
                "cta_strength": 0.8, "verdict": "keep", "reason": "winner",
            },
            {
                "index": 0, "hook": 0.4, "clarity": 0.4, "tone_fit": 0.4,
                "cta_strength": 0.4, "verdict": "drop", "reason": "weaker",
            },
        ]
    )

    tool, _client = _make_tool(
        accounts=accounts,
        vault=vault,
        anthropic_replies=[judge],
        openai_caller=boom_openai,
        save_caller=fake_save,
    )
    out = await tool.handler(
        {
            "to": "lead@example.com",
            "subject": "follow-up",
            "brief": "warm follow-up",
            "opus_candidates": ["Opus A", "Opus B"],
        },
        ToolContext(),
    )
    assert not out.is_error, out.content
    assert out.data["gpt_count"] == 0
    assert out.data["opus_count"] == 2
    assert out.data["winner_index"] == 1
    assert out.data["winner_source"] == "opus"
    assert out.data["winner_body"] == "Opus B"


@pytest.mark.asyncio
async def test_judge_failure_falls_back_to_first_opus(
    accounts: AccountsStore, vault: Vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Haiku errors, the tool saves opus_candidates[0] rather than
    failing the request. The operator still gets a usable draft."""
    _seed_user(accounts)
    _patch_creds(monkeypatch)

    async def fake_openai(api_key, model, system, user_message):
        return _gpt_drafts_reply(["GPT v1", "GPT v2"])

    async def fake_save(creds, raw_b64url, thread_id):
        return {"id": "draft-fallback", "message": {}}

    tool, client = _make_tool(
        accounts=accounts,
        vault=vault,
        anthropic_raises=True,
        openai_caller=fake_openai,
        save_caller=fake_save,
    )
    out = await tool.handler(
        {
            "to": "lead@example.com",
            "subject": "follow-up",
            "brief": "warm follow-up",
            "opus_candidates": ["Opus A is the fallback winner"],
        },
        ToolContext(),
    )
    assert not out.is_error, out.content
    assert out.data["winner_source"] == "opus"
    assert out.data["winner_index"] == 0
    assert out.data["winner_body"] == "Opus A is the fallback winner"
    assert out.data["score"] == 0.0
    # Judge call did fire (once) — and raised — even though we recovered.
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_no_openai_key_skips_gpt_pass(
    accounts: AccountsStore, vault: Vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``openai_api_key`` we never call the GPT seam — the
    Opus candidates carry the run by themselves."""
    _seed_user(accounts)
    _patch_creds(monkeypatch)

    openai_calls: list[Any] = []

    async def fake_openai(*args, **kwargs):
        openai_calls.append(args)
        raise AssertionError("openai_caller must NOT run with no key")

    async def fake_save(creds, raw_b64url, thread_id):
        return {"id": "draft-noai", "message": {}}

    judge = _judge_reply(
        [
            {
                "index": 0, "hook": 0.7, "clarity": 0.7, "tone_fit": 0.7,
                "cta_strength": 0.7, "verdict": "keep", "reason": "ok",
            },
            {
                "index": 1, "hook": 0.5, "clarity": 0.5, "tone_fit": 0.5,
                "cta_strength": 0.5, "verdict": "drop", "reason": "weaker",
            },
        ]
    )

    tool, _ = _make_tool(
        accounts=accounts,
        vault=vault,
        anthropic_replies=[judge],
        openai_caller=fake_openai,
        save_caller=fake_save,
        openai_api_key=None,
    )
    out = await tool.handler(
        {
            "to": "lead@example.com",
            "subject": "follow-up",
            "brief": "warm follow-up",
            "opus_candidates": ["Opus A", "Opus B"],
        },
        ToolContext(),
    )
    assert not out.is_error, out.content
    assert out.data["gpt_count"] == 0
    assert out.data["opus_count"] == 2
    assert openai_calls == []  # short-circuit before the seam


# ── Configurability ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_model_env_override(
    accounts: AccountsStore, vault: Vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact API string is operator-tunable via PILK_OPENAI_BON_MODEL."""
    _seed_user(accounts)
    _patch_creds(monkeypatch)
    monkeypatch.setenv("PILK_OPENAI_BON_MODEL", "gpt-5.5-2026-04")

    captured: dict[str, Any] = {}

    async def fake_openai(api_key, model, system, user_message):
        captured["model"] = model
        return _gpt_drafts_reply([])  # empty drafts, fall through

    async def fake_save(creds, raw_b64url, thread_id):
        return {"id": "d-1", "message": {}}

    judge = _judge_reply(
        [
            {
                "index": 0, "hook": 0.5, "clarity": 0.5, "tone_fit": 0.5,
                "cta_strength": 0.5, "verdict": "keep", "reason": "",
            }
        ]
    )

    tool, _ = _make_tool(
        accounts=accounts,
        vault=vault,
        anthropic_replies=[judge],
        openai_caller=fake_openai,
        save_caller=fake_save,
    )
    out = await tool.handler(
        {
            "to": "lead@x.com",
            "subject": "x",
            "brief": "test",
            "opus_candidates": ["only one"],
        },
        ToolContext(),
    )
    assert not out.is_error
    assert captured["model"] == "gpt-5.5-2026-04"
