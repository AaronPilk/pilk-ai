"""Manual / operator-curated intelligence ingest.

Used by ``POST /intelligence/sources/<id>/items`` to take an
operator-supplied URL (+ optional title / notes / publish date) and
funnel it through the same pipeline that auto-fetched items use:
canonical-URL dedup → keyword scoring → optional brain write.

This module owns:
  - One-shot, conservative title extraction from a URL (best-effort,
    failure-tolerant — if the page doesn't load, we still keep the
    submission).
  - The ``ingest_manual_item`` orchestration helper used by the
    HTTP route. Stays separate from ``pipeline.py`` because the
    pipeline is fetch-driven; manual items skip fetching entirely
    (or do at most ONE polite GET to grab a page <title>).

Strict scope (Batch 3C):
  - No aggressive scraping — at most one GET, 64 KiB cap, 8s
    timeout, custom User-Agent, ``follow_redirects=True``.
  - Operator notes never get auto-summarised by an LLM.
  - No Telegram alert, no plan creation, no autonomous action.
  - Brain write only when the resolved threshold (per-source >
    global) passes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from core.brain import Vault
from core.db import connect
from core.intelligence.brain_writer import BrainWriter
from core.intelligence.dedup import canonical_url
from core.intelligence.items import IntelItem, ItemStore
from core.intelligence.models import SourceSpec
from core.intelligence.scoring import KeywordScorer
from core.intelligence.topics import TopicRegistry
from core.logging import get_logger

log = get_logger("pilkd.intelligence.manual")

# Polite, bounded one-shot fetch when title isn't supplied.
_FETCH_TIMEOUT_S = 8.0
_FETCH_BODY_CAP = 64 * 1024  # 64 KiB — enough for <head>, never the
                             # full page body
_FETCH_USER_AGENT = "PILK-Intelligence/0.3 (+manual-ingest)"

# Conservative title patterns. We only trust HTML <title> / OG /
# Twitter card metadata in <head> — never parse the body.
_TITLE_RE = re.compile(
    r"<title[^>]*>([^<]+)</title>", re.IGNORECASE | re.DOTALL,
)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
    re.IGNORECASE,
)
_TWITTER_TITLE_RE = re.compile(
    r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)',
    re.IGNORECASE,
)

# Cap the URL-derived fallback title so a 2KB URL doesn't become the
# title display.
_MAX_FALLBACK_TITLE_CHARS = 200


@dataclass(frozen=True)
class ManualIngestOutcome:
    """Returned by :func:`ingest_manual_item` so the HTTP route can
    surface the same shape as a refresh outcome — keeps clients
    consistent."""

    ok: bool
    items_seen: int
    items_new: int
    items_dup: int
    items_brain_written: int
    item_id: str | None = None
    title_used: str | None = None
    title_source: str = "operator"  # "operator" | "fetched" | "url-fallback"
    fetch_attempted: bool = False
    fetch_succeeded: bool = False
    fetch_error: str | None = None
    score: int | None = None
    matched_topics: list[str] | None = None
    threshold_applied: int = 0
    threshold_source: str = "global"
    brain_path: str | None = None
    error: str | None = None


async def ingest_manual_item(
    *,
    source: SourceSpec,
    url: str,
    title: str | None,
    notes: str | None,
    published_at: str | None,
    topics: TopicRegistry,
    items: ItemStore,
    brain: Vault | None,
    global_threshold: int,
    http: httpx.AsyncClient | None = None,
) -> ManualIngestOutcome:
    """Single-item ingest path for operator-curated manual sources.

    Threshold precedence matches the pipeline:
      1. ``source.config['brain_min_score']`` (per-source override)
      2. ``global_threshold`` (engine default)
    """
    if source.kind != "manual":
        return ManualIngestOutcome(
            ok=False,
            items_seen=0,
            items_new=0,
            items_dup=0,
            items_brain_written=0,
            error=(
                f"source '{source.slug}' is kind '{source.kind}', "
                "not 'manual'. Manual ingest only accepts items for "
                "operator-curated sources."
            ),
        )

    threshold, threshold_source = _resolve_threshold(source, global_threshold)

    cleaned_url = (url or "").strip()
    if not cleaned_url:
        return ManualIngestOutcome(
            ok=False,
            items_seen=0,
            items_new=0,
            items_dup=0,
            items_brain_written=0,
            threshold_applied=threshold,
            threshold_source=threshold_source,
            error="url is required",
        )
    if not (
        cleaned_url.startswith("http://")
        or cleaned_url.startswith("https://")
    ):
        return ManualIngestOutcome(
            ok=False,
            items_seen=0,
            items_new=0,
            items_dup=0,
            items_brain_written=0,
            threshold_applied=threshold,
            threshold_source=threshold_source,
            error="url must start with http:// or https://",
        )

    try:
        canonical = canonical_url(cleaned_url)
    except ValueError as e:
        return ManualIngestOutcome(
            ok=False,
            items_seen=0,
            items_new=0,
            items_dup=0,
            items_brain_written=0,
            threshold_applied=threshold,
            threshold_source=threshold_source,
            error=f"could not canonicalise url: {e}",
        )

    # Resolve title. Operator wins; otherwise try a one-shot polite
    # fetch for the page <title>; otherwise fall back to a URL slug.
    operator_title = (title or "").strip()
    fetch_attempted = False
    fetch_succeeded = False
    fetch_error: str | None = None
    fetched_title: str | None = None
    if not operator_title:
        fetch_attempted = True
        fetched_title, fetch_error = await _fetch_page_title(
            cleaned_url, http=http,
        )
        fetch_succeeded = fetched_title is not None
    final_title, title_src = _pick_title(
        operator_title=operator_title,
        fetched_title=fetched_title,
        url=cleaned_url,
    )

    # Score over (title + notes + url) — the scorer's same surface.
    body_for_scoring = (notes or "").strip()
    scorer = await _build_scorer(source, topics)
    score_outcome = scorer.score(
        title=final_title,
        body=body_for_scoring,
        url=cleaned_url,
    )

    raw = {
        "manual": True,
        "operator_notes": (notes or "").strip()[:4000] if notes else None,
        "title_source": title_src,
        "fetch_attempted": fetch_attempted,
        "fetch_succeeded": fetch_succeeded,
        "fetch_error": fetch_error,
    }

    stored, is_new = await items.upsert_fetched(
        source_id=source.id,
        title=final_title,
        url=cleaned_url,
        body=body_for_scoring,
        external_id=canonical,
        published_at=(published_at or "").strip() or None,
        raw=raw,
        summary=(notes or "").strip()[:500] if notes else None,
    )

    if not is_new:
        # Existing row already has its score / brain path / status.
        return ManualIngestOutcome(
            ok=True,
            items_seen=1,
            items_new=0,
            items_dup=1,
            items_brain_written=0,
            item_id=stored.id,
            title_used=final_title,
            title_source=title_src,
            fetch_attempted=fetch_attempted,
            fetch_succeeded=fetch_succeeded,
            fetch_error=fetch_error,
            score=stored.score,
            matched_topics=list(stored.score_dimensions.keys()),
            threshold_applied=threshold,
            threshold_source=threshold_source,
            brain_path=stored.brain_path,
        )

    # New item: persist score + (conditionally) write to brain.
    await _apply_score(items, stored, score_outcome)

    brain_written = 0
    brain_path: str | None = None
    if (
        brain is not None
        and score_outcome.score >= threshold
    ):
        try:
            writer = BrainWriter(brain)
            write = writer.write(
                item=stored,
                source=source,
                body=body_for_scoring or final_title,
                matched_topics=score_outcome.matched_topics,
            )
            if not write.skipped and write.path:
                await _record_brain_path(items, stored.id, write.path)
                brain_written = 1
                brain_path = write.path
        except Exception as e:  # noqa: BLE001 — defensive
            log.warning(
                "intel_manual_brain_write_failed",
                item_id=stored.id,
                error=str(e),
            )

    return ManualIngestOutcome(
        ok=True,
        items_seen=1,
        items_new=1,
        items_dup=0,
        items_brain_written=brain_written,
        item_id=stored.id,
        title_used=final_title,
        title_source=title_src,
        fetch_attempted=fetch_attempted,
        fetch_succeeded=fetch_succeeded,
        fetch_error=fetch_error,
        score=score_outcome.score,
        matched_topics=score_outcome.matched_topics,
        threshold_applied=threshold,
        threshold_source=threshold_source,
        brain_path=brain_path,
    )


# ── helpers ──────────────────────────────────────────────────────


def _resolve_threshold(
    source: SourceSpec, global_default: int,
) -> tuple[int, str]:
    """Mirrors :meth:`IntelligencePipeline._resolve_threshold` so
    the two paths agree. Lives here (not imported) to avoid pulling
    in the pipeline module's full dependency footprint."""
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
    return global_default, "global"


async def _build_scorer(
    source: SourceSpec, topics: TopicRegistry,
) -> KeywordScorer:
    all_topics = await topics.list_topics()
    relevant = [
        t for t in all_topics
        if t.project_slug is None
        or t.project_slug == source.project_slug
    ]
    return KeywordScorer(relevant)


async def _apply_score(
    items: ItemStore, item: IntelItem, outcome,
) -> None:
    import json

    new_status = "scored" if outcome.score > 0 else "stored"
    async with connect(items.db_path) as conn:
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
    items: ItemStore, item_id: str, brain_path: str,
) -> None:
    async with connect(items.db_path) as conn:
        await conn.execute(
            "UPDATE intel_items SET brain_path = ? WHERE id = ?",
            (brain_path, item_id),
        )
        await conn.commit()


async def _fetch_page_title(
    url: str,
    *,
    http: httpx.AsyncClient | None = None,
) -> tuple[str | None, str | None]:
    """Polite one-shot fetch for the page <title>. Returns
    ``(title, error)`` — either a parsed title or a short error
    string the caller can record. Never raises on network failure;
    the operator's submission still goes through."""
    headers = {
        "User-Agent": _FETCH_USER_AGENT,
        "Accept": "text/html, application/xhtml+xml; q=0.9, */*; q=0.5",
    }
    owns_client = http is None
    client = http or httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT_S,
        follow_redirects=True,
    )
    try:
        try:
            resp = await client.get(url, headers=headers)
        except httpx.HTTPError as e:
            return None, f"network error: {type(e).__name__}: {e}"

        ctype = (resp.headers.get("content-type") or "").lower()
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        if "html" not in ctype and "xml" not in ctype:
            # Not a parseable page — don't try to extract a title.
            return None, f"unsupported content-type: {ctype.split(';')[0]}"

        body = resp.content[:_FETCH_BODY_CAP]
    finally:
        if owns_client:
            await client.aclose()

    # Try OG / Twitter card first (richer); fall back to <title>.
    text = body.decode("utf-8", errors="replace")
    for pat in (_OG_TITLE_RE, _TWITTER_TITLE_RE, _TITLE_RE):
        m = pat.search(text)
        if m:
            cleaned = _normalise_title(m.group(1))
            if cleaned:
                return cleaned, None
    return None, "no <title> or open-graph tag found in head"


def _pick_title(
    *,
    operator_title: str,
    fetched_title: str | None,
    url: str,
) -> tuple[str, str]:
    if operator_title:
        return _normalise_title(operator_title), "operator"
    if fetched_title:
        return fetched_title, "fetched"
    return _url_to_title(url), "url-fallback"


def _normalise_title(raw: str) -> str:
    cleaned = " ".join((raw or "").split())
    if len(cleaned) > _MAX_FALLBACK_TITLE_CHARS:
        cleaned = cleaned[:_MAX_FALLBACK_TITLE_CHARS] + "…"
    return cleaned or "(untitled)"


def _url_to_title(url: str) -> str:
    """Last-resort title — the URL itself, trimmed. Better than
    'untitled' because it gives the operator something recognisable
    when scrolling the brain folder."""
    cleaned = (url or "").strip()
    if not cleaned:
        return "(untitled)"
    # Strip scheme + trailing slash for readability.
    visible = cleaned.split("://", 1)[-1].rstrip("/")
    return _normalise_title(visible) or "(untitled)"


__all__ = ["ManualIngestOutcome", "ingest_manual_item"]
