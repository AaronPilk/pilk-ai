"""Tests for the boot-time brain auto-ingest task.

Covers:
- run_once over a real tmp-path Claude Code tree writes notes into
  the vault
- empty or missing root returns a clean zero-summary rather than
  crashing
- the max_projects cap is respected
- vault write failures land in the errors list but don't raise
- spawn returns an awaitable Task whose return value matches
  run_once
- _wrapped swallows unexpected exceptions so a bad ingester never
  kills the daemon
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from core.brain import Vault, auto_ingest


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8",
    )


def _seed_projects_tree(root: Path, slugs: list[str]) -> None:
    """Build a minimal ~/.claude/projects/ tree the ingester can
    parse. Each project gets one session with one user + one
    assistant turn."""
    for slug in slugs:
        proj = root / slug
        _write_jsonl(
            proj / "session.jsonl",
            [
                {
                    "type": "user",
                    "message": {"content": f"hi from {slug}"},
                    "timestamp": "2026-04-20T10:00:00Z",
                },
                {
                    "type": "assistant",
                    "message": {"content": f"hello {slug}"},
                    "timestamp": "2026-04-20T10:00:10Z",
                },
            ],
        )


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    v = Vault(tmp_path / "brain")
    v.ensure_initialized()
    return v


# ── run_once ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_once_writes_one_note_per_project(
    vault: Vault, tmp_path: Path,
) -> None:
    claude_root = tmp_path / "projects"
    _seed_projects_tree(claude_root, ["-Users-aaron-ai", "-Users-aaron-brand"])
    summary = await auto_ingest.run_once(vault, root=claude_root)
    assert summary["projects_scanned"] == 2
    assert summary["written"] == 2
    # And the notes actually landed in the vault.
    files = list((vault.root / "ingested" / "claude-code").glob("*.md"))
    assert len(files) == 2


@pytest.mark.asyncio
async def test_run_once_handles_missing_root(
    vault: Vault, tmp_path: Path,
) -> None:
    """Fresh install where ~/.claude/projects/ doesn't exist yet.
    Don't crash, don't write anything, return a clean zero-
    summary."""
    missing = tmp_path / "no-such-dir"
    summary = await auto_ingest.run_once(vault, root=missing)
    assert summary["projects_scanned"] == 0
    assert summary["written"] == 0


@pytest.mark.asyncio
async def test_run_once_empty_tree_is_clean(
    vault: Vault, tmp_path: Path,
) -> None:
    """The dir exists but has no project slugs yet."""
    empty = tmp_path / "projects"
    empty.mkdir()
    summary = await auto_ingest.run_once(vault, root=empty)
    assert summary["projects_scanned"] == 0
    assert summary["written"] == 0
    assert summary["errors"] == []


@pytest.mark.asyncio
async def test_run_once_respects_max_projects(
    vault: Vault, tmp_path: Path,
) -> None:
    claude_root = tmp_path / "projects"
    _seed_projects_tree(
        claude_root,
        [f"-Users-aaron-p{i}" for i in range(5)],
    )
    summary = await auto_ingest.run_once(
        vault, root=claude_root, max_projects=2,
    )
    # Scanner + cap both respected — the summary reports the
    # CAPPED count (post-cap), and exactly that many notes land.
    assert summary["projects_scanned"] == 2
    files = list((vault.root / "ingested" / "claude-code").glob("*.md"))
    assert len(files) == 2


@pytest.mark.asyncio
async def test_run_once_collects_write_errors_without_raising(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write failure for one project should not kill the whole
    ingest — the failure lands in summary['errors'] and we continue
    on to the next project."""
    claude_root = tmp_path / "projects"
    _seed_projects_tree(claude_root, ["-proj-ok", "-proj-bad"])

    original_write = vault.write
    call_count: dict[str, int] = {"n": 0}

    def flaky_write(path: str, body: str) -> Any:
        """Fail the first vault.write; succeed on the second. Drives
        the assertion that run_once logs the failure to errors + keeps
        going rather than aborting."""
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("disk full")
        return original_write(path, body)

    monkeypatch.setattr(vault, "write", flaky_write)
    summary = await auto_ingest.run_once(vault, root=claude_root)
    # One write succeeded, one raised but was caught.
    assert summary["written"] == 1
    assert len(summary["errors"]) == 1
    assert "disk full" in summary["errors"][0]


# ── spawn + _wrapped ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_returns_task_and_awaits_run_once(
    vault: Vault, tmp_path: Path,
) -> None:
    claude_root = tmp_path / "projects"
    _seed_projects_tree(claude_root, ["-Users-aaron-solo"])
    task = auto_ingest.spawn(vault, root=claude_root)
    assert isinstance(task, asyncio.Task)
    summary = await task
    assert summary["written"] == 1


@pytest.mark.asyncio
async def test_wrapped_swallows_unexpected_exceptions(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spawn wrapper must never propagate — failure of the
    ingester can't be allowed to kill the daemon event loop."""
    def blow_up(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("ingester detonated")

    monkeypatch.setattr(auto_ingest, "run_once", blow_up)
    task = auto_ingest.spawn(vault, root=tmp_path / "x")
    result = await task
    assert "error" in result
    assert "detonated" in result["error"]
