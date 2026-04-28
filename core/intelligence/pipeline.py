"""Fetch → store → score → brain-write pipeline.

Single source of truth for what happens when a source gets
refreshed, whether the trigger is the manual HTTP endpoint or the
background daemon. Keeping the logic in one place means the daemon
can't drift from the operator-driven path.

Strict batch-2 behaviour:
  - Fetches via the kind-dispatched fetcher
  - Stores items with content-hash + canonical-URL dedup
  - Scores items using the keyword scorer (no LLM)
  - Writes a markdown note to ``world/<date>/`` only when the
    score clears the configured threshold AND the brain writer is
    available
  - Records a fetch_run row
  - Updates the source's last_checked / last_status / failure
    counters
  - Never touches existing brain files. Never sends notifications.
  - Never fires plans. No autonomous action.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from core.brain import Vault
from core.db import connect
from core.intelligence.brain_writer import BrainWriter
from core.intelligence.fetchers import (
    NotImplementedFetchError,
    fetch_for_source,
)
from core.intelligence.fetchers.base import FetchError
from core.intelligence.items import IntelItem, ItemStore
from core.intelligence.models import SourceSpec
from core.intelligence.scoring import KeywordScorer, ScoreOutcome
from core.intelligence.sources import SourceRegistry
from core.intelligence.topics import TopicRegistry
from core.logging import get_logger

log = get_logger("pilkd.intelligence.pipeline")


@dataclass
class PipelineRunOutcome:
    run_id: str
    source_id: str
    ok: bool
    items_seen: int = 0
    items_new: int = 0
    items_dup: int = 0
    items_brain_written: int = 0
    # In dry-run mode this is the count of items that *would* have
    # been written to the brain at the resolved threshold but
    # weren't because dry_run=True.
    items_would_brain_write: int = 0
    # The threshold actually applied for this run — useful in the
    # response so the operator can see whether the per-source
    # override or the global default kicked in.
    threshold_applied: int = 0
    threshold_source: str = "global"  # "source" | "global"
    dry_run: bool = False
    note: str | None = None
    error: str | None = None


class IntelligencePipeline:
    """Stateless coordinator. Holds references to the SQLite-backed
    services + brain writer + topics for scoring. One instance per
    daemon / per request — cheap to construct.
    """

    def __init__(
        self,
        *,
        sources: SourceRegistry,
        topics: TopicRegistry,
        items: ItemStore,
        brain: Vault | None,
        brain_write_threshold: int = 30,
    ) -> None:
        self._sources = sources
        self._topics = topics
        self._items = items
        self._brain = brain
        self._writer: BrainWriter | None = (
            BrainWriter(brain) if brain is not None else None
        )
        self._brain_threshold = max(0, int(brain_write_threshold))

    async def run_source(
        self,
        source: SourceSpec,
        *,
        http: httpx.AsyncClient | None = None,
        dry_run: bool = False,
    ) -> PipelineRunOutcome:
        """Fetch + dedup-store + score one source. When ``dry_run``
        is True, items still land in SQLite (so dedup state stays
        accurate for future calls) but no brain note ever gets
        written — instead the outcome reports
        ``items_would_brain_write`` so the operator can preview the
        effect of a threshold change without creating files.

        Threshold precedence per call:
          1. ``source.config['min_score']`` (per-source override)
          2. ``self._brain_threshold`` (global default)
        Both are bounded to 0-100. The resolved value + which
        layer won is recorded in the outcome.
        """
        threshold, threshold_source = self._resolve_threshold(source)
        run_id = await self._items.start_run(source.id)
        try:
            try:
                result = await fetch_for_source(source, http=http)
            except NotImplementedFetchError as e:
                await self._items.finish_run(
                    run_id, status="not_implemented", error=str(e),
                )
                await self._sources.record_fetch_outcome(
                    source.id, status="not_implemented",
                )
                return PipelineRunOutcome(
                    run_id=run_id,
                    source_id=source.id,
                    ok=False,
                    error=str(e),
                )
            except FetchError as e:
                await self._items.finish_run(
                    run_id, status="error", error=str(e),
                )
                await self._sources.record_fetch_outcome(
                    source.id, status="error", increment_failures=True,
                )
                return PipelineRunOutcome(
                    run_id=run_id,
                    source_id=source.id,
                    ok=False,
                    error=str(e),
                )

            seen = len(result.items)
            new_count = 0
            dup_count = 0
            brain_count = 0
            would_brain_count = 0

            scorer = await self._build_scorer(source)
            for fi in result.items:
                try:
                    stored, is_new = await self._items.upsert_fetched(
                        source_id=source.id,
                        title=fi.title,
                        url=fi.url,
                        body=fi.body,
                        external_id=fi.external_id,
                        published_at=fi.published_at,
                        raw=fi.raw,
                    )
                except Exception as e:  # noqa: BLE001 — defensive
                    log.warning(
                        "intel_pipeline_upsert_failed",
                        source_id=source.id,
                        error=str(e),
                    )
                    continue

                if not is_new:
                    dup_count += 1
                    continue

                new_count += 1

                # Score + (optionally) write to brain. Only
                # scoring + writing on NEW items so already-stored
                # items don't get rewritten.
                outcome = scorer.score(
                    title=fi.title,
                    body=fi.body,
                    url=fi.url,
                )
                await self._apply_score(stored, outcome)

                if outcome.score >= threshold:
                    if dry_run:
                        # Count what WOULD have been written. Do not
                        # touch the filesystem — that's the contract.
                        would_brain_count += 1
                    elif self._writer is not None:
                        try:
                            write = self._writer.write(
                                item=stored,
                                source=source,
                                body=fi.body,
                                matched_topics=outcome.matched_topics,
                            )
                            if not write.skipped and write.path:
                                await self._record_brain_path(
                                    stored.id, write.path
                                )
                                brain_count += 1
                        except Exception as e:  # noqa: BLE001
                            log.warning(
                                "intel_brain_write_failed",
                                item_id=stored.id,
                                error=str(e),
                            )

            await self._items.finish_run(
                run_id,
                status="ok",
                items_seen=seen,
                items_new=new_count,
                items_dup=dup_count,
            )
            await self._sources.record_fetch_outcome(
                source.id,
                status="ok",
                etag=result.etag,
                last_modified=result.last_modified,
                reset_failures=True,
            )
            return PipelineRunOutcome(
                run_id=run_id,
                source_id=source.id,
                ok=True,
                items_seen=seen,
                items_new=new_count,
                items_dup=dup_count,
                items_brain_written=brain_count,
                items_would_brain_write=would_brain_count,
                threshold_applied=threshold,
                threshold_source=threshold_source,
                dry_run=dry_run,
                note=result.note,
            )
        except Exception as e:  # noqa: BLE001 — defensive
            log.exception(
                "intel_pipeline_unhandled", source_id=source.id,
            )
            await self._items.finish_run(
                run_id, status="error", error=str(e),
            )
            await self._sources.record_fetch_outcome(
                source.id, status="error", increment_failures=True,
            )
            return PipelineRunOutcome(
                run_id=run_id,
                source_id=source.id,
                ok=False,
                error=str(e),
            )

    # ── helpers ──────────────────────────────────────────────────

    def _resolve_threshold(
        self, source: SourceSpec,
    ) -> tuple[int, str]:
        """Pick the brain-write threshold for a source.

        Per-source ``config['brain_min_score']`` wins when present
        and valid (integer 0-100); otherwise the global default
        applies. Returns ``(threshold, source)`` where ``source`` is
        the string ``"source"`` or ``"global"`` so the run outcome
        can tell the operator which layer triggered.

        We use ``brain_min_score`` (not ``min_score``) because some
        fetchers — notably the Hacker News fetcher — already use
        ``config['min_score']`` for source-specific filtering (HN's
        own story-score floor). Sharing the key would conflate
        "filter HN stories below X HN points" with "write items
        below X relevance score to brain" — different concepts.

        Validation is deliberately lenient: a malformed value
        (string, negative, > 100, missing) silently falls back to
        the global default rather than failing the whole fetch.
        """
        config = source.config or {}
        raw = config.get("brain_min_score")
        if raw is not None:
            try:
                v = int(raw)
            except (TypeError, ValueError):
                v = None
            else:
                if 0 <= v <= 100:
                    return v, "source"
                log.warning(
                    "intel_brain_min_score_out_of_range",
                    source_id=source.id,
                    value=v,
                )
        return self._brain_threshold, "global"

    async def _build_scorer(self, source: SourceSpec) -> KeywordScorer:
        """Build a scorer over topics that apply to this source. We
        include topics with no project (cross-project) plus topics
        scoped to the same project as the source."""
        all_topics = await self._topics.list_topics()
        relevant = [
            t for t in all_topics
            if t.project_slug is None
            or t.project_slug == source.project_slug
        ]
        return KeywordScorer(relevant)

    async def _apply_score(
        self, item: IntelItem, outcome: ScoreOutcome,
    ) -> None:
        """Update the item row with the score outcome. Writes
        directly because the scorer's columns aren't part of the
        store's standard upsert path."""
        new_status = (
            "scored" if outcome.score > 0 else "stored"
        )
        async with connect(self._items.db_path) as conn:
            await conn.execute(
                """UPDATE intel_items
                       SET score = ?,
                           score_reason = ?,
                           score_dimensions_json = ?,
                           status = ?
                     WHERE id = ?""",
                (
                    outcome.score,
                    outcome.reason,
                    json.dumps(outcome.dimensions, separators=(",", ":")),
                    new_status,
                    item.id,
                ),
            )
            await conn.commit()
        item.score = outcome.score
        item.score_reason = outcome.reason
        item.score_dimensions = outcome.dimensions
        item.status = new_status

    async def _record_brain_path(
        self, item_id: str, brain_path: str,
    ) -> None:
        async with connect(self._items.db_path) as conn:
            await conn.execute(
                "UPDATE intel_items SET brain_path = ? WHERE id = ?",
                (brain_path, item_id),
            )
            await conn.commit()


__all__ = ["IntelligencePipeline", "PipelineRunOutcome"]
