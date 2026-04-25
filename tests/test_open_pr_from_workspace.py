"""Tests for ``open_pr_from_workspace`` — the tool that turns a
working-tree change into a GitHub PR.

The factory exposes two seams (``git_runner``, ``pr_creator``) so
no real subprocess or HTTP runs in tests. The repo path is a
real ``tmp_path`` so the ``.git`` existence check passes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from core.tools.builtin.git_pr import (
    make_open_pr_from_workspace_tool,
)
from core.tools.registry import ToolContext

# ── Helpers ─────────────────────────────────────────────────────


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A fake git checkout — just enough for the ``.git`` directory
    check; real git operations are stubbed."""
    repo = tmp_path / "pilk-repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def _ensure_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")


def _make_runner(
    *,
    status_lines: str = "M core/api/app.py\n",
    fail_on: tuple[str, ...] = (),
):
    """Build a fake git runner that returns ``status_lines`` for
    ``git status`` and empty strings for everything else, optionally
    failing on the named subcommands."""

    calls: list[list[str]] = []

    async def runner(repo_path: Path, args: list[str]) -> str:
        calls.append(args)
        if args and args[0] in fail_on:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=["git", *args],
                stderr=f"simulated {args[0]} failure",
            )
        if args[:2] == ["status", "--porcelain"]:
            return status_lines
        return ""

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _make_pr_creator(
    *,
    fail: bool = False,
    payload: dict[str, Any] | None = None,
):
    calls: list[dict[str, Any]] = []

    async def creator(token, repo, title, body, head, base):
        calls.append(
            {
                "token": token, "repo": repo, "title": title,
                "body": body, "head": head, "base": base,
            }
        )
        if fail:
            raise RuntimeError("github 422 unprocessable")
        return payload or {
            "html_url": (
                f"https://github.com/{repo}/pull/42"
            ),
            "number": 42,
        }

    creator.calls = calls  # type: ignore[attr-defined]
    return creator


# ── Validation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_title_returns_error(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ensure_token(monkeypatch)
    tool = make_open_pr_from_workspace_tool(
        fake_repo,
        git_runner=_make_runner(),
        pr_creator=_make_pr_creator(),
    )
    out = await tool.handler(
        {"body": "what changed and why"}, ToolContext(),
    )
    assert out.is_error
    assert "title" in out.content.lower()


@pytest.mark.asyncio
async def test_missing_body_returns_error(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ensure_token(monkeypatch)
    tool = make_open_pr_from_workspace_tool(
        fake_repo,
        git_runner=_make_runner(),
        pr_creator=_make_pr_creator(),
    )
    out = await tool.handler(
        {"title": "fix something"}, ToolContext(),
    )
    assert out.is_error
    assert "body" in out.content.lower()


@pytest.mark.asyncio
async def test_missing_token_returns_error(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PILK_GITHUB_TOKEN", raising=False)
    tool = make_open_pr_from_workspace_tool(
        fake_repo,
        git_runner=_make_runner(),
        pr_creator=_make_pr_creator(),
    )
    out = await tool.handler(
        {"title": "fix x", "body": "..."}, ToolContext(),
    )
    assert out.is_error
    assert "GITHUB_TOKEN" in out.content


@pytest.mark.asyncio
async def test_clean_tree_short_circuits(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ``git status`` means nothing to commit — the tool must
    refuse rather than open an empty PR."""
    _ensure_token(monkeypatch)
    runner = _make_runner(status_lines="")  # clean tree
    creator = _make_pr_creator()
    tool = make_open_pr_from_workspace_tool(
        fake_repo, git_runner=runner, pr_creator=creator,
    )
    out = await tool.handler(
        {"title": "fix x", "body": "..."}, ToolContext(),
    )
    assert out.is_error
    assert "clean" in out.content.lower()
    # Must not have proceeded to commit / push / PR.
    sub_args = [c[0] for c in runner.calls if c]
    assert "commit" not in sub_args
    assert "push" not in sub_args
    assert creator.calls == []


@pytest.mark.asyncio
async def test_repo_path_must_be_a_git_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ensure_token(monkeypatch)
    not_a_repo = tmp_path / "no-git-here"
    not_a_repo.mkdir()
    tool = make_open_pr_from_workspace_tool(
        not_a_repo,
        git_runner=_make_runner(),
        pr_creator=_make_pr_creator(),
    )
    out = await tool.handler(
        {"title": "fix x", "body": "..."}, ToolContext(),
    )
    assert out.is_error
    assert "git checkout" in out.content


# ── Happy path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_creates_branch_commits_pushes_opens_pr(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ensure_token(monkeypatch)
    runner = _make_runner()
    creator = _make_pr_creator()
    tool = make_open_pr_from_workspace_tool(
        fake_repo, git_runner=runner, pr_creator=creator,
    )
    out = await tool.handler(
        {
            "title": "fix the brain page autosave bug",
            "body": "## What changed\nThe Brain page now saves...",
        },
        ToolContext(),
    )
    assert not out.is_error, out.content
    assert out.data["pr_url"] == (
        "https://github.com/AaronPilk/pilk-ai/pull/42"
    )
    assert out.data["pr_number"] == 42
    assert out.data["base"] == "main"
    assert out.data["branch"].startswith("claude/")
    assert "fix-the-brain-page-autosave-bug" in out.data["branch"]

    # Verify the git command sequence.
    seq = [c[0] for c in runner.calls if c]
    assert seq == [
        "status",
        "fetch",
        "checkout",
        "add",
        "commit",
        "push",
    ]
    # Push targets the new branch with --set-upstream.
    push_call = next(c for c in runner.calls if c[:1] == ["push"])
    assert "--set-upstream" in push_call
    assert "origin" in push_call

    # PR creation called with the right args.
    assert len(creator.calls) == 1
    pr_args = creator.calls[0]
    assert pr_args["repo"] == "AaronPilk/pilk-ai"
    assert pr_args["title"] == "fix the brain page autosave bug"
    assert pr_args["base"] == "main"
    assert pr_args["head"] == out.data["branch"]


@pytest.mark.asyncio
async def test_happy_path_with_explicit_repo_and_base(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ensure_token(monkeypatch)
    runner = _make_runner()
    creator = _make_pr_creator(
        payload={
            "html_url": "https://github.com/x/y/pull/7",
            "number": 7,
        },
    )
    tool = make_open_pr_from_workspace_tool(
        fake_repo, git_runner=runner, pr_creator=creator,
    )
    out = await tool.handler(
        {
            "title": "experimental change",
            "body": "trial",
            "repo": "x/y",
            "base": "develop",
            "branch_slug": "trial-run",
        },
        ToolContext(),
    )
    assert not out.is_error, out.content
    assert out.data["repo"] == "x/y"
    assert out.data["base"] == "develop"
    assert "trial-run" in out.data["branch"]
    assert creator.calls[0]["repo"] == "x/y"
    assert creator.calls[0]["base"] == "develop"


# ── Failure-mode rollback / messaging ──────────────────────────


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_branch(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``git commit`` fails, the tool must abort the branch (so
    the next attempt isn't blocked by a stale half-baked branch)
    and surface the error."""
    _ensure_token(monkeypatch)
    runner = _make_runner(fail_on=("commit",))
    creator = _make_pr_creator()
    tool = make_open_pr_from_workspace_tool(
        fake_repo, git_runner=runner, pr_creator=creator,
    )
    out = await tool.handler(
        {"title": "fix x", "body": "..."}, ToolContext(),
    )
    assert out.is_error
    assert "Commit failed" in out.content
    # Rollback fired: a checkout-back-to-base + branch -D should be
    # in the call sequence.
    seq = [tuple(c[:2]) for c in runner.calls if c]
    assert ("checkout", "main") in seq
    # PR was never attempted.
    assert creator.calls == []


@pytest.mark.asyncio
async def test_push_failure_surfaces_credential_hint(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ensure_token(monkeypatch)
    runner = _make_runner(fail_on=("push",))
    creator = _make_pr_creator()
    tool = make_open_pr_from_workspace_tool(
        fake_repo, git_runner=runner, pr_creator=creator,
    )
    out = await tool.handler(
        {"title": "fix x", "body": "..."}, ToolContext(),
    )
    assert out.is_error
    assert "Push" in out.content
    assert "credentials" in out.content.lower()
    assert creator.calls == []


@pytest.mark.asyncio
async def test_pr_create_failure_after_push_succeeds(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If push went through but the PR API call fails, the tool
    surfaces the situation honestly so PILK can tell the operator
    the branch is up but the PR didn't open."""
    _ensure_token(monkeypatch)
    runner = _make_runner()
    creator = _make_pr_creator(fail=True)
    tool = make_open_pr_from_workspace_tool(
        fake_repo, git_runner=runner, pr_creator=creator,
    )
    out = await tool.handler(
        {"title": "fix x", "body": "..."}, ToolContext(),
    )
    assert out.is_error
    assert "Branch is on GitHub" in out.content
    assert "manually" in out.content
    # Push was attempted (and "succeeded" per the runner).
    seq = [c[0] for c in runner.calls if c]
    assert "push" in seq


@pytest.mark.asyncio
async def test_fetch_failure_does_not_abort(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``git fetch`` is best-effort; if it fails (offline, etc.) we
    keep going off whatever local base we have. The push will
    surface a real error if the branch is genuinely behind."""
    _ensure_token(monkeypatch)
    runner = _make_runner(fail_on=("fetch",))
    creator = _make_pr_creator()
    tool = make_open_pr_from_workspace_tool(
        fake_repo, git_runner=runner, pr_creator=creator,
    )
    out = await tool.handler(
        {"title": "fix x", "body": "..."}, ToolContext(),
    )
    assert not out.is_error, out.content
    assert out.data["pr_url"] == (
        "https://github.com/AaronPilk/pilk-ai/pull/42"
    )


# ── Surface ─────────────────────────────────────────────────────


def test_tool_surface(
    fake_repo: Path,
) -> None:
    tool = make_open_pr_from_workspace_tool(fake_repo)
    assert tool.name == "open_pr_from_workspace"
    assert set(tool.input_schema["required"]) == {"title", "body"}
    # All optional fields are advertised so the planner can use them.
    props = tool.input_schema["properties"]
    for key in ("base", "branch_slug", "repo", "repo_path"):
        assert key in props
