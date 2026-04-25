"""``open_pr_from_workspace`` — turn a working-tree change into a
GitHub PR with one tool call.

The workflow PILK runs when the operator asks for a code change:

1. Operator says "fix X" / "make Y faster" / "add Z" via chat or
   Telegram.
2. PILK calls ``code_task`` with ``scope="repo"`` pointing at this
   repo. Claude Code edits files locally.
3. PILK calls THIS tool with a plain-English title and body. The
   tool creates a branch from ``origin/main``, commits everything
   in the working tree, pushes it, and opens a PR via the GitHub
   REST API. Returns the PR URL.
4. PILK pings the operator on Telegram with the URL in plain
   English ("PR is up — tap here to merge").

Risk class is EXEC_LOCAL: the work is local git + a single REST
call to a known repo PILK already controls. The operator
authorized this whole class of action by giving PILK access to
modify itself; gating every PR behind an approval click defeats
the purpose. The PR itself is the safety rail — the operator
sees the diff and clicks merge from their phone.

Failure modes are surfaced verbatim so PILK can either retry,
clean up the branch, or tell the operator what went wrong in
plain English.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.git_pr")

DEFAULT_BASE = "main"
DEFAULT_REPO = "AaronPilk/pilk-ai"
GITHUB_API = "https://api.github.com"
GIT_TIMEOUT_S = 60.0
HTTPX_TIMEOUT_S = 30.0
BRANCH_PREFIX = "claude/"
MAX_TITLE_CHARS = 70
MAX_BODY_CHARS = 65000

# Test seams — both default to real subprocess / httpx but tests
# override them to avoid touching the actual repo / GitHub.
GitRunner = Callable[[Path, list[str]], Awaitable[str]]
PRCreator = Callable[
    [str, str, str, str, str, str], Awaitable[dict[str, Any]]
]

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, *, limit: int = 40) -> str:
    cleaned = _SLUG_RE.sub("-", text.lower()).strip("-")[:limit].strip("-")
    return cleaned or "change"


def _branch_name(slug: str) -> str:
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{BRANCH_PREFIX}{ts}-{slug}"


def _resolve_token() -> str | None:
    return (
        os.getenv("GITHUB_TOKEN")
        or os.getenv("PILK_GITHUB_TOKEN")
        or None
    )


async def _default_git_runner(repo_path: Path, args: list[str]) -> str:
    """Run a git subcommand in the given repo. Raises with stderr
    on non-zero exit so callers can surface a clean error."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(repo_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=GIT_TIMEOUT_S,
        )
    except TimeoutError as e:
        proc.kill()
        raise RuntimeError(f"git {args[0]} timed out") from e
    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="replace").strip()
        raise subprocess.CalledProcessError(
            returncode=proc.returncode or 1,
            cmd=["git", *args],
            output=stdout,
            stderr=err,
        )
    return (stdout or b"").decode("utf-8", errors="replace")


async def _default_pr_creator(
    token: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
) -> dict[str, Any]:
    """POST /repos/<owner>/<repo>/pulls. Returns the JSON response."""
    async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT_S) as client:
        resp = await client.post(
            f"{GITHUB_API}/repos/{repo}/pulls",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "pilkd",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
            },
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"GitHub PR create failed "
                f"(HTTP {resp.status_code}): {resp.text[:400]}"
            )
        return resp.json()


def make_open_pr_from_workspace_tool(
    repo_root: Path,
    *,
    default_repo: str = DEFAULT_REPO,
    git_runner: GitRunner | None = None,
    pr_creator: PRCreator | None = None,
) -> Tool:
    """Factory. ``repo_root`` is the absolute path on disk of the
    PILK repo (the daemon's own checkout). Tests inject ``git_runner``
    and ``pr_creator`` to avoid real subprocess + network calls."""

    git_run = git_runner or _default_git_runner
    pr_create = pr_creator or _default_pr_creator

    async def _handler(args: dict, ctx: ToolContext) -> ToolOutcome:
        title = str(args.get("title") or "").strip()
        body = str(args.get("body") or "").strip()
        base = str(args.get("base") or DEFAULT_BASE).strip() or DEFAULT_BASE
        repo = str(args.get("repo") or default_repo).strip() or default_repo
        repo_path_arg = args.get("repo_path")
        repo_path = (
            Path(str(repo_path_arg)).expanduser().resolve()
            if repo_path_arg
            else repo_root
        )
        slug_arg = str(args.get("branch_slug") or "").strip()
        slug = _slugify(slug_arg or title)

        if not title:
            return ToolOutcome(
                content=(
                    "open_pr_from_workspace needs a 'title' (the PR "
                    "title — short, plain English)."
                ),
                is_error=True,
            )
        if len(title) > MAX_TITLE_CHARS:
            return ToolOutcome(
                content=(
                    f"title is too long ({len(title)} chars; max "
                    f"{MAX_TITLE_CHARS}). Tighten it."
                ),
                is_error=True,
            )
        if not body:
            return ToolOutcome(
                content=(
                    "open_pr_from_workspace needs a 'body' (the PR "
                    "description — what changed and why, in markdown)."
                ),
                is_error=True,
            )
        if len(body) > MAX_BODY_CHARS:
            return ToolOutcome(
                content=(
                    f"body is too long ({len(body)} chars; cap "
                    f"{MAX_BODY_CHARS}). Trim or move detail to a "
                    "linked note."
                ),
                is_error=True,
            )
        if not repo_path.exists() or not (repo_path / ".git").exists():
            return ToolOutcome(
                content=(
                    f"repo_path '{repo_path}' is not a git checkout. "
                    "Pass an absolute path to a working tree."
                ),
                is_error=True,
            )

        token = _resolve_token()
        if not token:
            return ToolOutcome(
                content=(
                    "GITHUB_TOKEN (or PILK_GITHUB_TOKEN) is not set, "
                    "so no PR can be opened. Add a personal access "
                    "token with 'repo' scope to your environment, "
                    "then retry."
                ),
                is_error=True,
            )

        # Verify there's actually something to commit. ``git status
        # --porcelain`` prints nothing on a clean tree.
        try:
            status = await git_run(repo_path, ["status", "--porcelain"])
        except (subprocess.CalledProcessError, RuntimeError) as e:
            return ToolOutcome(
                content=f"git status failed: {e}", is_error=True,
            )
        if not status.strip():
            return ToolOutcome(
                content=(
                    "Working tree is clean — nothing to commit. Run "
                    "code_task first to make the changes you want to "
                    "ship, then call this tool."
                ),
                is_error=True,
            )

        branch = _branch_name(slug)

        # Best-effort: fetch the base so we branch from a fresh tip.
        # If fetch fails (offline, auth), proceed anyway off whatever
        # local base we have — the push will fail clearly later if
        # the branch is out of sync.
        try:
            await git_run(repo_path, ["fetch", "origin", base])
        except (subprocess.CalledProcessError, RuntimeError) as e:
            log.warning(
                "open_pr_fetch_failed", base=base, error=str(e)
            )

        try:
            await git_run(
                repo_path, ["checkout", "-b", branch],
            )
        except (subprocess.CalledProcessError, RuntimeError) as e:
            return ToolOutcome(
                content=(
                    f"Couldn't create branch {branch}: {e}. The branch "
                    "name may already exist or the repo state is "
                    "unexpected."
                ),
                is_error=True,
            )

        async def _abort_branch() -> None:
            """Best-effort rollback: get back to base, drop the branch.
            Failure here is logged but doesn't override the original
            error — we don't want to stomp the real reason."""
            try:
                await git_run(repo_path, ["checkout", base])
                await git_run(repo_path, ["branch", "-D", branch])
            except Exception:
                log.exception("open_pr_rollback_failed", branch=branch)

        try:
            await git_run(repo_path, ["add", "-A"])
            await git_run(
                repo_path,
                ["commit", "-m", title, "-m", body],
            )
        except (subprocess.CalledProcessError, RuntimeError) as e:
            await _abort_branch()
            return ToolOutcome(
                content=f"Commit failed: {e}", is_error=True,
            )

        try:
            await git_run(
                repo_path,
                ["push", "--set-upstream", "origin", branch],
            )
        except (subprocess.CalledProcessError, RuntimeError) as e:
            return ToolOutcome(
                content=(
                    f"Push to origin failed: {e}. The branch was "
                    f"committed locally but never reached GitHub. "
                    "Most likely cause: missing or stale GitHub "
                    "credentials in the daemon's environment."
                ),
                is_error=True,
            )

        try:
            pr_payload = await pr_create(
                token, repo, title, body, branch, base,
            )
        except (httpx.HTTPError, RuntimeError) as e:
            return ToolOutcome(
                content=(
                    f"Branch is on GitHub ({branch}) but the PR open "
                    f"call failed: {e}. You can open the PR manually "
                    f"in the operator's browser."
                ),
                is_error=True,
                data={"branch": branch, "base": base, "repo": repo},
            )

        pr_url = pr_payload.get("html_url") or ""
        pr_number = pr_payload.get("number")
        log.info(
            "open_pr_completed",
            repo=repo,
            branch=branch,
            base=base,
            pr_number=pr_number,
        )
        return ToolOutcome(
            content=(
                f"PR opened: {pr_url}\n\n"
                f"The operator can review the diff on GitHub and "
                f"click 'Merge pull request' from their phone or "
                f"laptop. Branch: {branch} (off {base})."
            ),
            data={
                "pr_url": pr_url,
                "pr_number": pr_number,
                "branch": branch,
                "base": base,
                "repo": repo,
            },
        )

    return Tool(
        name="open_pr_from_workspace",
        description=(
            "After making code changes via code_task, call this to "
            "ship them as a GitHub Pull Request. The tool creates a "
            "branch from main, commits everything in the working "
            "tree, pushes the branch, and opens a PR via GitHub's "
            "API. Returns the PR URL — pass it to the operator on "
            "Telegram with a plain-English message ('PR is up, tap "
            "to merge: <url>'). Auto-allowed (EXEC_LOCAL): no "
            "approval click. The PR itself is the safety rail — the "
            "operator reviews the diff and merges from their phone. "
            "Requires GITHUB_TOKEN with 'repo' scope on the daemon "
            "host. Fails cleanly with no commit if the working tree "
            "is empty."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "PR title. Short, plain English, conventional-"
                        "commit prefix optional ('fix:', 'feat:'). "
                        f"Max {MAX_TITLE_CHARS} chars."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "PR description. Markdown. Cover what changed "
                        "and why. The operator may not read this — "
                        "engineers do. Be a little technical here."
                    ),
                },
                "base": {
                    "type": "string",
                    "description": (
                        "Base branch (default 'main'). The PR opens "
                        "against this."
                    ),
                },
                "branch_slug": {
                    "type": "string",
                    "description": (
                        "Optional — explicit slug for the new branch "
                        "name. Auto-derived from title if omitted."
                    ),
                },
                "repo": {
                    "type": "string",
                    "description": (
                        f"GitHub owner/repo (default '{default_repo}'). "
                        "Override only when working on a different "
                        "repo."
                    ),
                },
                "repo_path": {
                    "type": "string",
                    "description": (
                        "Absolute path to the local checkout (default: "
                        "PILK's own repo on the daemon host). Override "
                        "when running this for a different repo."
                    ),
                },
            },
            "required": ["title", "body"],
        },
        risk=RiskClass.EXEC_LOCAL,
        handler=_handler,
    )
