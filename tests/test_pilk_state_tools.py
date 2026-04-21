"""Self-introspection tools — registry / git-log / open PRs / deploys.

Registry + git-log tools run locally (real ToolRegistry, real `git log`
under the repo's own root). The GitHub-backed tools are covered with
httpx.MockTransport so the suite doesn't hit the public API.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from core.policy.risk import RiskClass
from core.tools.builtin.pilk_state import (
    make_pilk_deploy_status_tool,
    make_pilk_open_prs_tool,
    make_pilk_recent_changes_tool,
    make_pilk_registered_tools_tool,
)
from core.tools.registry import (
    Tool,
    ToolContext,
    ToolOutcome,
    ToolRegistry,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _noop_tool(name: str) -> Tool:
    async def handler(_a: dict[str, Any], _c: ToolContext) -> ToolOutcome:
        return ToolOutcome(content="ok")

    return Tool(
        name=name,
        description=f"{name} description",
        input_schema={"type": "object", "properties": {}},
        risk=RiskClass.READ,
        handler=handler,
    )


# ── pilk_registered_tools ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_registered_tools_lists_all_when_no_filter() -> None:
    reg = ToolRegistry()
    reg.register(_noop_tool("alpha"))
    reg.register(_noop_tool("bravo"))
    tool = make_pilk_registered_tools_tool(reg)
    out = await tool.handler({}, ToolContext())
    assert not out.is_error
    names = [t["name"] for t in out.data["tools"]]
    assert names == ["alpha", "bravo"]  # sorted
    assert out.data["count"] == 2


@pytest.mark.asyncio
async def test_registered_tools_filter_is_case_insensitive() -> None:
    reg = ToolRegistry()
    reg.register(_noop_tool("gmail_send"))
    reg.register(_noop_tool("fs_read"))
    reg.register(_noop_tool("fs_write"))
    tool = make_pilk_registered_tools_tool(reg)
    out = await tool.handler({"filter": "FS"}, ToolContext())
    assert out.data["count"] == 2
    assert {t["name"] for t in out.data["tools"]} == {"fs_read", "fs_write"}


@pytest.mark.asyncio
async def test_registered_tools_empty_filter_returns_noop_message() -> None:
    reg = ToolRegistry()
    reg.register(_noop_tool("alpha"))
    tool = make_pilk_registered_tools_tool(reg)
    out = await tool.handler({"filter": "zzznomatch"}, ToolContext())
    assert not out.is_error
    assert "No tools match" in out.content


# ── pilk_recent_changes ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recent_changes_reads_repo_git_log() -> None:
    tool = make_pilk_recent_changes_tool(REPO_ROOT)
    out = await tool.handler({"limit": 3}, ToolContext())
    assert not out.is_error, out.content
    assert out.data["count"] > 0
    first = out.data["commits"][0]
    assert set(first.keys()) >= {"sha", "date", "author", "subject"}
    # sha is a short hash
    assert 6 <= len(first["sha"]) <= 40


@pytest.mark.asyncio
async def test_recent_changes_clamps_limit(tmp_path: Path) -> None:
    tool = make_pilk_recent_changes_tool(REPO_ROOT)
    # Out-of-range limit should not raise — it clamps.
    out = await tool.handler({"limit": "not-a-number"}, ToolContext())
    assert not out.is_error


@pytest.mark.asyncio
async def test_recent_changes_non_git_dir_errors(tmp_path: Path) -> None:
    tool = make_pilk_recent_changes_tool(tmp_path)
    out = await tool.handler({"limit": 1}, ToolContext())
    assert out.is_error
    assert "git log failed" in out.content


# ── pilk_open_prs ────────────────────────────────────────────────────


def _mock_client(handler) -> None:
    """Patch httpx.AsyncClient to route through MockTransport."""


@pytest.mark.asyncio
async def test_open_prs_formats_api_response(monkeypatch) -> None:
    sample_pulls = [
        {
            "number": 42,
            "title": "fix thing",
            "draft": False,
            "user": {"login": "someone"},
            "created_at": "2026-04-01T00:00:00Z",
            "updated_at": "2026-04-02T00:00:00Z",
            "html_url": "https://github.com/AaronPilk/pilk-ai/pull/42",
        },
        {
            "number": 43,
            "title": "wip",
            "draft": True,
            "user": {"login": "bot"},
            "created_at": "2026-04-03T00:00:00Z",
            "updated_at": "2026-04-03T00:00:00Z",
            "html_url": "https://github.com/AaronPilk/pilk-ai/pull/43",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/pulls")
        assert request.url.params["state"] == "open"
        return httpx.Response(200, json=sample_pulls)

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs.pop("timeout", None)
        return orig(transport=transport)

    monkeypatch.setattr("core.tools.builtin.pilk_state.httpx.AsyncClient", patched)

    tool = make_pilk_open_prs_tool()
    out = await tool.handler({}, ToolContext())
    assert not out.is_error
    assert out.data["count"] == 2
    assert "[draft]" in out.content
    assert out.data["pulls"][1]["draft"] is True


@pytest.mark.asyncio
async def test_open_prs_surfaces_http_error(monkeypatch) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient
    monkeypatch.setattr(
        "core.tools.builtin.pilk_state.httpx.AsyncClient",
        lambda *a, **kw: orig(transport=transport),
    )
    tool = make_pilk_open_prs_tool()
    out = await tool.handler({}, ToolContext())
    assert out.is_error
    assert "404" in out.content


# ── pilk_deploy_status ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deploy_status_filters_to_known_workflows(monkeypatch) -> None:
    # Newest-first mix of runs across deploy + ci + some unrelated
    # workflow. We expect one row per known workflow, preserving
    # declared order (deploy-ui, deploy-portal, ci).
    runs = {
        "workflow_runs": [
            {
                "path": ".github/workflows/deploy-ui.yml",
                "name": "deploy-ui",
                "status": "completed",
                "conclusion": "success",
                "head_sha": "aaa111122223333",
                "created_at": "2026-04-21T23:50:00Z",
                "html_url": "u1",
            },
            {
                "path": ".github/workflows/unrelated.yml",
                "name": "unrelated",
                "status": "completed",
                "conclusion": "success",
                "head_sha": "bbb222",
                "created_at": "2026-04-21T23:49:00Z",
                "html_url": "u2",
            },
            {
                "path": ".github/workflows/ci.yml",
                "name": "ci",
                "status": "completed",
                "conclusion": "success",
                "head_sha": "ccc333",
                "created_at": "2026-04-21T23:48:00Z",
                "html_url": "u3",
            },
            {
                "path": ".github/workflows/deploy-portal.yml",
                "name": "deploy-portal",
                "status": "in_progress",
                "conclusion": None,
                "head_sha": "ddd444",
                "created_at": "2026-04-21T23:47:00Z",
                "html_url": "u4",
            },
            # Older ci run — should be skipped in favour of the newer
            # one seen first.
            {
                "path": ".github/workflows/ci.yml",
                "name": "ci",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": "eee555",
                "created_at": "2026-04-21T23:30:00Z",
                "html_url": "u5",
            },
        ],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/actions/runs")
        assert req.url.params["branch"] == "main"
        return httpx.Response(200, json=runs)

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient
    monkeypatch.setattr(
        "core.tools.builtin.pilk_state.httpx.AsyncClient",
        lambda *a, **kw: orig(transport=transport),
    )
    tool = make_pilk_deploy_status_tool()
    out = await tool.handler({}, ToolContext())
    assert not out.is_error
    workflows = [r["workflow"] for r in out.data["runs"]]
    assert workflows == ["deploy-ui.yml", "deploy-portal.yml", "ci.yml"]
    # Head shas are truncated to 7 chars for the UI.
    assert out.data["runs"][0]["head_sha"] == "aaa1111"
    # Second ci run (failure) should be dropped — keep the newer one.
    ci_row = next(r for r in out.data["runs"] if r["workflow"] == "ci.yml")
    assert ci_row["conclusion"] == "success"


@pytest.mark.asyncio
async def test_deploy_status_empty_runs_returns_friendly_message(
    monkeypatch,
) -> None:
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, json={"workflow_runs": []}),
    )
    orig = httpx.AsyncClient
    monkeypatch.setattr(
        "core.tools.builtin.pilk_state.httpx.AsyncClient",
        lambda *a, **kw: orig(transport=transport),
    )
    tool = make_pilk_deploy_status_tool()
    out = await tool.handler({}, ToolContext())
    assert not out.is_error
    assert out.data["runs"] == []
    assert "No recent deploy" in out.content
