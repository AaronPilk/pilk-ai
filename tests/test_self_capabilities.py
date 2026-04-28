"""Tests for the self-capabilities refresher.

The refresher's job: at boot, compare git HEAD to the hash recorded
in ``standing-instructions/pilk-capabilities.md`` and (if they
differ) regenerate the note via one Anthropic call.

Tests use a stub Anthropic client + a stub git repo so they run
fast and free.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.brain import Vault
from core.self_capabilities import SelfCapabilitiesRefresher
from core.self_capabilities.refresher import NOTE_RELATIVE_PATH


# ── stub anthropic client ────────────────────────────────────────


@dataclass
class _Block:
    text: str
    type: str = "text"


@dataclass
class _Resp:
    content: list[_Block]


class _StubMessages:
    def __init__(self, body: str) -> None:
        self._body = body
        self.calls: list[dict] = []

    async def create(self, **kw):
        self.calls.append(kw)
        return _Resp(content=[_Block(text=self._body)])


class _StubClient:
    def __init__(self, body: str = "## What I can do today\n\nAll the things.") -> None:
        self.messages = _StubMessages(body)


# ── fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    root = tmp_path / "PILK-brain"
    (root / "standing-instructions").mkdir(parents=True)
    return root


@pytest.fixture
def vault(vault_root: Path) -> Vault:
    return Vault(vault_root)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Initialise a tiny git repo so the refresher's git probe
    works without poking the real PILK repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=repo, check=True,
    )
    (repo / "README.md").write_text("hello", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=repo, check=True,
    )
    return repo


def _commit(repo: Path, msg: str) -> str:
    """Make a fresh commit and return its short SHA."""
    (repo / "README.md").write_text(
        f"hello {msg}", encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "."], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", msg], cwd=repo, check=True,
    )
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo,
    ).decode("utf-8").strip()


# ── tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_writes_note_when_no_prior(
    vault: Vault, vault_root: Path, repo_root: Path,
) -> None:
    """First-ever run — no prior note, refresh writes one."""
    client = _StubClient(body="## What I can do today\n\nfresh body")

    class _FakeRegistry:
        def all(self): return []

    class _FakeSettings:
        intelligence_daemon_enabled = False
        computer_control_enabled = ""
        computer_control_daily_limit = 20

    refresher = SelfCapabilitiesRefresher(
        vault=vault, repo_root=repo_root, anthropic_client=client,
    )
    out = await refresher.refresh_if_stale(
        registry=_FakeRegistry(), settings=_FakeSettings(),
    )
    assert out.status == "refreshed"
    assert out.head_commit
    assert out.previous_commit is None
    assert out.note_path == NOTE_RELATIVE_PATH
    note = (vault_root / NOTE_RELATIVE_PATH).read_text()
    assert "fresh body" in note
    assert f"last_refreshed_commit: {out.head_commit}" in note
    assert "auto_refresh: true" in note


@pytest.mark.asyncio
async def test_refresh_short_circuits_when_head_unchanged(
    vault: Vault, vault_root: Path, repo_root: Path,
) -> None:
    """Second run with the same HEAD: refresh should no-op."""
    client = _StubClient()

    class _FakeRegistry:
        def all(self): return []

    class _FakeSettings:
        intelligence_daemon_enabled = False
        computer_control_enabled = ""
        computer_control_daily_limit = 20

    refresher = SelfCapabilitiesRefresher(
        vault=vault, repo_root=repo_root, anthropic_client=client,
    )
    first = await refresher.refresh_if_stale(
        registry=_FakeRegistry(), settings=_FakeSettings(),
    )
    assert first.status == "refreshed"
    # Second call with no commit between — should short-circuit.
    second = await refresher.refresh_if_stale(
        registry=_FakeRegistry(), settings=_FakeSettings(),
    )
    assert second.status == "up_to_date"
    assert second.head_commit == first.head_commit
    # Stub client should only have been called once.
    assert len(client.messages.calls) == 1


@pytest.mark.asyncio
async def test_refresh_fires_again_after_new_commit(
    vault: Vault, vault_root: Path, repo_root: Path,
) -> None:
    """A new commit should trigger a re-summary."""
    client = _StubClient()

    class _FakeRegistry:
        def all(self): return []

    class _FakeSettings:
        intelligence_daemon_enabled = False
        computer_control_enabled = ""
        computer_control_daily_limit = 20

    refresher = SelfCapabilitiesRefresher(
        vault=vault, repo_root=repo_root, anthropic_client=client,
    )
    first = await refresher.refresh_if_stale(
        registry=_FakeRegistry(), settings=_FakeSettings(),
    )
    assert first.status == "refreshed"
    new_head = _commit(repo_root, "new feature")
    second = await refresher.refresh_if_stale(
        registry=_FakeRegistry(), settings=_FakeSettings(),
    )
    assert second.status == "refreshed"
    assert second.head_commit == new_head
    assert second.previous_commit == first.head_commit
    assert len(client.messages.calls) == 2


@pytest.mark.asyncio
async def test_refresh_force_skips_short_circuit(
    vault: Vault, vault_root: Path, repo_root: Path,
) -> None:
    client = _StubClient()

    class _FakeRegistry:
        def all(self): return []

    class _FakeSettings:
        intelligence_daemon_enabled = False
        computer_control_enabled = ""
        computer_control_daily_limit = 20

    refresher = SelfCapabilitiesRefresher(
        vault=vault, repo_root=repo_root, anthropic_client=client,
    )
    await refresher.refresh_if_stale(
        registry=_FakeRegistry(), settings=_FakeSettings(),
    )
    out = await refresher.refresh_if_stale(
        registry=_FakeRegistry(), settings=_FakeSettings(), force=True,
    )
    assert out.status == "refreshed"
    assert len(client.messages.calls) == 2


@pytest.mark.asyncio
async def test_refresh_skipped_when_no_client(
    vault: Vault, repo_root: Path,
) -> None:
    """No Anthropic key → no refresh, no error."""

    class _FakeRegistry:
        def all(self): return []

    class _FakeSettings:
        intelligence_daemon_enabled = False
        computer_control_enabled = ""
        computer_control_daily_limit = 20

    refresher = SelfCapabilitiesRefresher(
        vault=vault, repo_root=repo_root, anthropic_client=None,
    )
    out = await refresher.refresh_if_stale(
        registry=_FakeRegistry(), settings=_FakeSettings(),
    )
    assert out.status == "skipped_no_anthropic"


@pytest.mark.asyncio
async def test_refresh_handles_llm_failure(
    vault: Vault, vault_root: Path, repo_root: Path,
) -> None:
    """If the Anthropic call blows up, the existing note is left
    in place and the outcome is 'failed' — never crashes boot."""

    class _BoomClient:
        class _Boom:
            async def create(self, **kw):
                raise RuntimeError("anthropic exploded")
        messages = _Boom()

    class _FakeRegistry:
        def all(self): return []

    class _FakeSettings:
        intelligence_daemon_enabled = False
        computer_control_enabled = ""
        computer_control_daily_limit = 20

    refresher = SelfCapabilitiesRefresher(
        vault=vault, repo_root=repo_root,
        anthropic_client=_BoomClient(),
    )
    out = await refresher.refresh_if_stale(
        registry=_FakeRegistry(), settings=_FakeSettings(),
    )
    assert out.status == "failed"
    assert "exploded" in (out.error or "")
    # No note was written.
    assert not (vault.root / NOTE_RELATIVE_PATH).exists()


@pytest.mark.asyncio
async def test_note_carries_tool_inventory(
    vault: Vault, vault_root: Path, repo_root: Path,
) -> None:
    """Tool inventory feeds the LLM prompt and tool count lands in
    the note frontmatter."""
    captured_prompt: list[str] = []

    class _CapturingMessages:
        async def create(self, **kw):
            captured_prompt.append(kw["messages"][0]["content"])
            return _Resp(
                content=[_Block(text="## What I can do\n\nbody")]
            )

    class _CapturingClient:
        messages = _CapturingMessages()

    class _Tool:
        def __init__(self, name: str, risk_value: str) -> None:
            self.name = name
            self.description = f"does {name}"
            class _R:
                value = risk_value
            self.risk = _R()

    class _FakeRegistry:
        def all(self):
            return [_Tool("brain_search", "READ"),
                    _Tool("telegram_notify", "COMMS")]

    class _FakeSettings:
        intelligence_daemon_enabled = False
        computer_control_enabled = ""
        computer_control_daily_limit = 20

    refresher = SelfCapabilitiesRefresher(
        vault=vault, repo_root=repo_root,
        anthropic_client=_CapturingClient(),
    )
    out = await refresher.refresh_if_stale(
        registry=_FakeRegistry(), settings=_FakeSettings(),
    )
    assert out.status == "refreshed"
    assert "brain_search" in captured_prompt[0]
    assert "telegram_notify" in captured_prompt[0]
    note = (vault.root / NOTE_RELATIVE_PATH).read_text()
    assert "tool_count: 2" in note
