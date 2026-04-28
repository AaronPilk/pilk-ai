"""Read-only intelligence tools exposed to agents.

Currently exports one tool:

  ``intelligence_digest_read`` — wraps ``ItemStore.digest`` so a
  delegated agent (Master Reporting in particular) can pull recent
  intelligence items without going through the HTTP loopback. Same
  filters as the ``GET /intelligence/digest`` endpoint; same
  read-only contract.

The tool deliberately does NOT expose mutation surfaces — no
``upsert``, no ``mark_seen``, no ``record_brain_path``, etc. If a
future batch wants those, they should land as separate tools so the
risk class is auditable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.intelligence.items import ItemStore
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.intelligence.tools")

# Hard ceiling on rows returned per call. The endpoint's max is 200;
# matched here so an agent can't accidentally fan-out into a
# multi-thousand-row response that bloats the next planner turn.
_DIGEST_MAX_LIMIT = 200
_DIGEST_DEFAULT_LIMIT = 50


def make_intelligence_digest_read_tool(db_path: Path) -> Tool:
    """Construct the read-only digest tool with a live ``db_path``.

    Same shape as Sentinel's ``make_intel_source_health_rule`` —
    factory function so the live DB path is closed over at wiring
    time and the tool needs no external state at call time.
    """
    store = ItemStore(db_path)

    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        # Validate + normalise inputs. Be lenient: an agent that
        # passes a malformed limit/min_score should get a bounded
        # default rather than a tool failure that costs a planner
        # turn to recover from.
        since = _opt_str(args.get("since"))
        project = _opt_str(args.get("project"))
        include_global = bool(args.get("include_global"))
        source_slug = _opt_str(args.get("source"))
        topic = _opt_str(args.get("topic"))

        min_score = args.get("min_score")
        if min_score is not None:
            try:
                min_score = max(0, min(100, int(min_score)))
            except (TypeError, ValueError):
                min_score = None

        raw_limit = args.get("limit")
        if raw_limit is None:
            limit = _DIGEST_DEFAULT_LIMIT
        else:
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                limit = _DIGEST_DEFAULT_LIMIT
        limit = max(1, min(limit, _DIGEST_MAX_LIMIT))

        try:
            entries = await store.digest(
                since=since,
                project=project,
                include_global=include_global,
                source_slug=source_slug,
                topic=topic,
                min_score=min_score,
                limit=limit,
            )
        except Exception as e:  # noqa: BLE001 — defensive
            log.warning(
                "intelligence_digest_read_failed", error=str(e),
            )
            return ToolOutcome(
                content=f"intelligence_digest_read failed: {e}",
                is_error=True,
            )

        # Render two surfaces: a compact human-readable text body
        # (what the planner mostly looks at) and the structured data
        # in ``data`` (what downstream tooling can parse). The data
        # block carries the resolved filters too so the agent can
        # confirm what was applied.
        items_data = [
            {
                "item_id": e.item_id,
                "title": e.title,
                "url": e.url,
                "source_slug": e.source_slug,
                "source_label": e.source_label,
                "source_kind": e.source_kind,
                "project_slug": e.project_slug,
                "published_at": e.published_at,
                "fetched_at": e.fetched_at,
                "score": e.score,
                "score_reason": e.score_reason,
                "brain_path": e.brain_path,
                "status": e.status,
                "matched_topics": e.matched_topics,
            }
            for e in entries
        ]

        text = _render_digest_text(items_data)

        return ToolOutcome(
            content=text,
            data={
                "filters": {
                    "since": since,
                    "project": project,
                    "include_global": include_global,
                    "source": source_slug,
                    "topic": topic,
                    "min_score": min_score,
                    "limit": limit,
                },
                "count": len(items_data),
                "items": items_data,
            },
        )

    return Tool(
        name="intelligence_digest_read",
        description=(
            "READ-ONLY. Pull a digest of recent intelligence items "
            "PILK has collected (RSS, HN, GitHub releases, arXiv, "
            "operator-curated manual sources). Same filter set as "
            "GET /intelligence/digest:\n"
            " - since (ISO 8601 or partial date — '24h ago' must be "
            "supplied as an actual ISO timestamp by the caller)\n"
            " - project (slug — narrows to one project)\n"
            " - include_global (when project is set, also include "
            "items from sources with no project_slug)\n"
            " - source (filter by source slug)\n"
            " - topic (filter to items that matched this topic)\n"
            " - min_score (0-100; default no floor)\n"
            " - limit (1-200; default 50)\n"
            "Returns newest-first list with title, url, source, "
            "score, brain_path, matched_topics. Use this when Aaron "
            "asks for a daily/weekly intelligence brief or asks "
            "'what's new in the world?'. Never writes anything."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": (
                        "ISO 8601 timestamp or partial date. Items "
                        "with fetched_at >= this value are returned. "
                        "For 'last 24h' compute now-1d at call time; "
                        "for 'last 7d' compute now-7d."
                    ),
                },
                "project": {
                    "type": "string",
                    "description": (
                        "Project slug to narrow the brief to one "
                        "active project (e.g. 'skyway-sales')."
                    ),
                },
                "include_global": {
                    "type": "boolean",
                    "description": (
                        "When project is set, also include items "
                        "from sources with no project_slug. Default "
                        "false."
                    ),
                },
                "source": {
                    "type": "string",
                    "description": "Source slug filter.",
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Watchlist topic slug (e.g. 'ai-agents')."
                    ),
                },
                "min_score": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": (
                        "Floor on relevance score (0-100). Default "
                        "no floor; common briefs use 50."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _DIGEST_MAX_LIMIT,
                    "description": (
                        f"Cap on items returned (default "
                        f"{_DIGEST_DEFAULT_LIMIT}, max "
                        f"{_DIGEST_MAX_LIMIT})."
                    ),
                },
            },
        },
        risk=RiskClass.READ,
        handler=handler,
    )


# ── helpers ──────────────────────────────────────────────────────


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _render_digest_text(items: list[dict[str, Any]]) -> str:
    """Compact, planner-friendly rendering. The planner reads this
    string; the structured ``data`` block stays available for
    programmatic uses (e.g. building the markdown brief)."""
    if not items:
        return "0 intelligence items match the requested filters."
    lines = [f"{len(items)} intelligence item(s):"]
    for it in items:
        score = it.get("score")
        score_str = f"score={score}" if score is not None else "score=-"
        topics = ", ".join(it.get("matched_topics") or [])
        scope = (
            f"project={it['project_slug']}"
            if it.get("project_slug")
            else "project=(global)"
        )
        title = (it.get("title") or "(untitled)")[:140]
        lines.append(
            f"- [{it['source_slug']}] {scope} {score_str} "
            f"topics={topics or '-'}\n"
            f"   {title}\n"
            f"   url: {it['url']}"
        )
        if it.get("brain_path"):
            lines.append(f"   brain: {it['brain_path']}")
    return "\n".join(lines)


__all__ = ["make_intelligence_digest_read_tool"]
