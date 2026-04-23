"""ChatGPT index rebuild-on-upload hook.

The upload endpoint calls ``_rebuild_chatgpt_index`` after ingesting a
ChatGPT export so memory hydration picks up the fresh conversations on
the very next turn, instead of waiting for the nightly 03:00 rebuild
(or a pilkd restart). These tests cover the helper in isolation: it
should walk the vault's ``ingested/chatgpt/`` directory and return the
entry count, and swallow errors without raising.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.api.routes.brain import _rebuild_chatgpt_index


@pytest.mark.asyncio
async def test_rebuild_reads_freshly_written_conversation(
    tmp_path: Path,
) -> None:
    """A conversation written to ingested/chatgpt/ immediately before
    the rebuild call shows up in the index entry count — proving the
    hook actually re-scans disk rather than using any cached view."""
    chatgpt_dir = tmp_path / "ingested" / "chatgpt"
    chatgpt_dir.mkdir(parents=True)
    (chatgpt_dir / "2026-04-23-hello.md").write_text(
        "# Hello from ChatGPT\n\nSome conversation body.\n",
        encoding="utf-8",
    )

    count = await _rebuild_chatgpt_index(tmp_path)
    assert count == 1

    # Adding a second file and rebuilding bumps the count — proves the
    # rebuild is idempotent rather than append-only.
    (chatgpt_dir / "2026-04-23-another.md").write_text(
        "# Another thread\n\nMore body.\n", encoding="utf-8",
    )
    count_after = await _rebuild_chatgpt_index(tmp_path)
    assert count_after == 2


@pytest.mark.asyncio
async def test_rebuild_empty_vault_returns_zero(tmp_path: Path) -> None:
    """Fresh vault (no ingested/chatgpt directory yet) rebuilds to a
    zero-entry index cleanly — the upload caller relies on this so a
    pre-first-upload state reports imported=N + index_entries=N from
    a clean slate."""
    count = await _rebuild_chatgpt_index(tmp_path)
    assert count == 0
