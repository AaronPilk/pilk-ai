"""pilk_* self-introspection tools.

Four read-only tools that let PILK inspect its own runtime state so
the operator can ask "what tools do you have?", "what did you ship
this week?", "are the deploys green?", or "what PRs are open on your
own repo?" without grep-ing the codebase.

  * pilk_registered_tools — snapshot of ToolRegistry
  * pilk_recent_changes   — last N `git log` entries on this repo
  * pilk_open_prs         — open PRs on AaronPilk/pilk-ai via GitHub API
  * pilk_deploy_status    — latest GitHub Actions run for deploy-ui /
                            deploy-portal / ci, filtered from the
                            workflow-runs endpoint

All four are RiskClass.READ / NET_READ — none mutate local or remote
state. The GitHub calls work unauthenticated against public endpoints
but pick up a ``GITHUB_TOKEN`` / ``PILK_GITHUB_TOKEN`` env var when
set for a 5 000-req/hour limit instead of 60.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx

from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome, ToolRegistry

log = get_logger("pilkd.tools.pilk_state")

# Default repo the introspection tools point at. Kept here rather than
# in settings because these tools are specifically about PILK looking
# at itself; a different deploy of the same codebase would rebuild from
# the same string anyway.
_DEFAULT_REPO = "AaronPilk/pilk-ai"
_GITHUB_API = "https://api.github.com"
_DEPLOY_WORKFLOWS = ("deploy-ui.yml", "deploy-portal.yml", "ci.yml")


# ── pilk_registered_tools ────────────────────────────────────────────


def make_pilk_registered_tools_tool(registry: ToolRegistry) -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        want = str(args.get("filter") or "").strip().lower()
        tools = registry.all()
        rows: list[dict[str, Any]] = []
        for t in sorted(tools, key=lambda x: x.name):
            if want and want not in t.name.lower():
                continue
            rows.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "risk": t.risk.value,
                }
            )
        if not rows:
            return ToolOutcome(
                content=(
                    f"No tools match filter '{want}'."
                    if want
                    else "No tools registered."
                ),
            )
        # Compact content for voice; structured data for the UI/chat.
        summary_lines = [f"{r['name']} [{r['risk']}]" for r in rows]
        return ToolOutcome(
            content=(
                f"{len(rows)} tool{'s' if len(rows) != 1 else ''} "
                f"registered:\n" + "\n".join(summary_lines)
            ),
            data={"count": len(rows), "tools": rows},
        )

    return Tool(
        name="pilk_registered_tools",
        description=(
            "List the tools PILK currently has registered at runtime "
            "— useful when the operator asks 'what can you do?' or "
            "'do you have a Slack tool yet?'. Optional `filter` "
            "substring matches against tool names (case-insensitive). "
            "Returns name + one-line description + risk class per "
            "tool. Read-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "description": (
                        "Optional case-insensitive substring. Omit to "
                        "list every registered tool."
                    ),
                },
            },
        },
        risk=RiskClass.READ,
        handler=handler,
    )


# ── pilk_recent_changes ──────────────────────────────────────────────


def make_pilk_recent_changes_tool(repo_root: Path) -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        limit_raw = args.get("limit", 20)
        try:
            limit = max(1, min(int(limit_raw), 200))
        except (TypeError, ValueError):
            limit = 20
        proc = await asyncio.create_subprocess_exec(
            "git",
            "log",
            f"-n{limit}",
            "--pretty=format:%h%x09%cI%x09%an%x09%s",
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return ToolOutcome(
                content=(
                    "git log failed: "
                    + (stderr.decode("utf-8", errors="replace") or "no stderr")
                ),
                is_error=True,
            )
        commits: list[dict[str, str]] = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.split("\t", 3)
            if len(parts) != 4:
                continue
            sha, date, author, subject = parts
            commits.append(
                {
                    "sha": sha,
                    "date": date,
                    "author": author,
                    "subject": subject,
                }
            )
        summary = "\n".join(
            f"{c['sha']} {c['date'][:10]} {c['subject']}" for c in commits
        )
        return ToolOutcome(
            content=(
                f"Last {len(commits)} commit"
                f"{'s' if len(commits) != 1 else ''} on main:\n{summary}"
                if commits
                else "No commits found."
            ),
            data={"count": len(commits), "commits": commits},
        )

    return Tool(
        name="pilk_recent_changes",
        description=(
            "Read the last N commits from this repo's git log — "
            "useful when the operator asks 'what did we ship today?' "
            "or 'when did the OAuth fix land?'. Returns sha + ISO "
            "date + author + subject per commit. Default limit 20, "
            "max 200. Read-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": (
                        "How many commits to return (newest first). "
                        "Defaults to 20."
                    ),
                },
            },
        },
        risk=RiskClass.READ,
        handler=handler,
    )


# ── pilk_open_prs ────────────────────────────────────────────────────


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "pilkd",
    }
    token = os.getenv("GITHUB_TOKEN") or os.getenv("PILK_GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def make_pilk_open_prs_tool(repo: str = _DEFAULT_REPO) -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        target = str(args.get("repo") or repo).strip()
        url = f"{_GITHUB_API}/repos/{target}/pulls?state=open&per_page=50"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=_github_headers())
        except httpx.HTTPError as e:
            return ToolOutcome(
                content=f"GitHub request failed: {e}",
                is_error=True,
            )
        if resp.status_code != 200:
            return ToolOutcome(
                content=(
                    f"GitHub returned {resp.status_code}: "
                    f"{resp.text[:200]}"
                ),
                is_error=True,
            )
        raw = resp.json()
        pulls: list[dict[str, Any]] = []
        for p in raw:
            pulls.append(
                {
                    "number": p.get("number"),
                    "title": p.get("title"),
                    "draft": bool(p.get("draft")),
                    "author": (p.get("user") or {}).get("login"),
                    "created_at": p.get("created_at"),
                    "updated_at": p.get("updated_at"),
                    "url": p.get("html_url"),
                }
            )
        if not pulls:
            return ToolOutcome(
                content=f"No open PRs on {target}.",
                data={"count": 0, "pulls": [], "repo": target},
            )
        lines = [
            f"#{p['number']} {'[draft] ' if p['draft'] else ''}{p['title']} "
            f"— @{p['author']}"
            for p in pulls
        ]
        return ToolOutcome(
            content=(
                f"{len(pulls)} open PR"
                f"{'s' if len(pulls) != 1 else ''} on {target}:\n"
                + "\n".join(lines)
            ),
            data={"count": len(pulls), "pulls": pulls, "repo": target},
        )

    return Tool(
        name="pilk_open_prs",
        description=(
            "List open PRs on the PILK repo via the GitHub API — "
            "useful when the operator asks 'what PRs are still in "
            f"flight?'. Defaults to {_DEFAULT_REPO}; pass `repo` as "
            "'owner/name' to point elsewhere. Works unauthenticated "
            "but respects GITHUB_TOKEN / PILK_GITHUB_TOKEN for rate "
            "limits. Read-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": (
                        "GitHub 'owner/name'. Defaults to "
                        f"'{_DEFAULT_REPO}'."
                    ),
                },
            },
        },
        risk=RiskClass.NET_READ,
        handler=handler,
    )


# ── pilk_deploy_status ───────────────────────────────────────────────


def make_pilk_deploy_status_tool(repo: str = _DEFAULT_REPO) -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        target = str(args.get("repo") or repo).strip()
        # Pull recent runs once, filter locally per workflow. Avoids
        # N GitHub requests when all four workflows have run in the
        # last 20 commits.
        url = (
            f"{_GITHUB_API}/repos/{target}/actions/runs"
            f"?per_page=30&branch=main"
        )
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=_github_headers())
        except httpx.HTTPError as e:
            return ToolOutcome(
                content=f"GitHub request failed: {e}",
                is_error=True,
            )
        if resp.status_code != 200:
            return ToolOutcome(
                content=(
                    f"GitHub returned {resp.status_code}: "
                    f"{resp.text[:200]}"
                ),
                is_error=True,
            )
        runs = (resp.json() or {}).get("workflow_runs") or []
        latest: dict[str, dict[str, Any]] = {}
        for run in runs:
            path = (run.get("path") or "").split("/")[-1]
            if path not in _DEPLOY_WORKFLOWS:
                continue
            if path in latest:
                # API returns newest-first, so keep the first one seen.
                continue
            latest[path] = {
                "workflow": path,
                "name": run.get("name"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "head_sha": (run.get("head_sha") or "")[:7],
                "created_at": run.get("created_at"),
                "url": run.get("html_url"),
            }
        rows = [latest.get(w) for w in _DEPLOY_WORKFLOWS if w in latest]
        if not rows:
            return ToolOutcome(
                content=(
                    f"No recent deploy / ci runs found on main for "
                    f"{target}."
                ),
                data={"runs": [], "repo": target},
            )
        lines = [
            f"{r['workflow']}: {r['conclusion'] or r['status']} "
            f"(sha {r['head_sha']})"
            for r in rows
        ]
        return ToolOutcome(
            content=(
                f"Deploy status on main for {target}:\n" + "\n".join(lines)
            ),
            data={"runs": rows, "repo": target},
        )

    return Tool(
        name="pilk_deploy_status",
        description=(
            "Latest deploy + ci status on main for the PILK repo via "
            "GitHub Actions. Returns the most recent run per "
            "workflow (deploy-ui.yml, deploy-portal.yml, ci.yml) "
            "with status / conclusion / head sha. Useful when the "
            "operator asks 'did the last deploy go through?'. "
            "Read-only; respects GITHUB_TOKEN / PILK_GITHUB_TOKEN."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": (
                        "GitHub 'owner/name'. Defaults to "
                        f"'{_DEFAULT_REPO}'."
                    ),
                },
            },
        },
        risk=RiskClass.NET_READ,
        handler=handler,
    )


__all__ = [
    "make_pilk_deploy_status_tool",
    "make_pilk_open_prs_tool",
    "make_pilk_recent_changes_tool",
    "make_pilk_registered_tools_tool",
]
