"""``brain_semantic_search`` — read-only semantic retrieval tool.

Wraps ``SemanticSearch`` so PILK and master agents can query the
vector index from inside a plan turn. Returns hits with brain_path,
heading, content snippet, project_slug, source_type, and score
so the planner can cite + open the source markdown for full context.

Risk class is READ — no writes, no deletes, no embedding spend on
the search path itself (each query is one tiny embed call). The
operator triggers an explicit reindex separately via the API.
"""

from __future__ import annotations

from typing import Any

from core.brain.vector.embedder import EmbeddingError
from core.brain.vector.search import SearchHit, SemanticSearch
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.brain.vector.tool")

_DEFAULT_LIMIT = 8
_MAX_LIMIT = 30


def make_brain_semantic_search_tool(search: SemanticSearch) -> Tool:
    """Construct the read-only semantic search tool with a live
    ``SemanticSearch`` instance closed over at wiring time."""

    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        query = (args.get("query") or "").strip()
        if not query:
            return ToolOutcome(
                content="brain_semantic_search requires a 'query'.",
                is_error=True,
            )
        project_slug = args.get("project_slug")
        if isinstance(project_slug, str):
            project_slug = project_slug.strip() or None
        else:
            project_slug = None
        source_type = args.get("source_type")
        if isinstance(source_type, str):
            source_type = source_type.strip() or None
        else:
            source_type = None
        try:
            limit = int(args.get("limit") or _DEFAULT_LIMIT)
        except (TypeError, ValueError):
            limit = _DEFAULT_LIMIT
        limit = max(1, min(limit, _MAX_LIMIT))
        min_score = args.get("min_score")
        if min_score is not None:
            try:
                min_score = float(min_score)
            except (TypeError, ValueError):
                min_score = None
        try:
            hits = await search.search(
                query,
                limit=limit,
                project_slug=project_slug,
                source_type=source_type,
                min_score=min_score,
            )
        except EmbeddingError as e:
            return ToolOutcome(
                content=f"brain_semantic_search failed: {e}",
                is_error=True,
            )
        except Exception as e:  # noqa: BLE001 — defensive
            log.warning(
                "brain_semantic_search_failed", error=str(e),
            )
            return ToolOutcome(
                content=f"brain_semantic_search failed: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=_render_text(hits),
            data={
                "filters": {
                    "query": query,
                    "limit": limit,
                    "project_slug": project_slug,
                    "source_type": source_type,
                    "min_score": min_score,
                },
                "count": len(hits),
                "hits": [_to_dict(h) for h in hits],
            },
        )

    return Tool(
        name="brain_semantic_search",
        description=(
            "READ-ONLY. Semantic search across PILK's brain vault — "
            "all markdown notes under ~/PILK-brain (persona, projects, "
            "world intelligence, standing instructions, ingested files).\n"
            " - query (required)\n"
            " - limit (1-30; default 8)\n"
            " - project_slug (narrow to one project, e.g. 'skyway-sales')\n"
            " - source_type (one of: persona, project, world, "
            "standing-instructions, ingested, daily, inbox, other)\n"
            " - min_score (0.0-1.0 cosine cutoff; omit for all hits)\n"
            "Returns ranked chunks with brain_path, heading, content "
            "snippet, project_slug, and similarity score. Use this when "
            "Aaron asks 'what do I know about <topic>?' or 'find any "
            "notes relevant to <idea>' — it finds matches by meaning, "
            "not just keywords. Read full notes via brain_note_read."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text query. Vector search "
                    "matches by meaning so paraphrased questions hit.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_LIMIT,
                    "description": (
                        f"Max hits (default {_DEFAULT_LIMIT}, "
                        f"max {_MAX_LIMIT})."
                    ),
                },
                "project_slug": {
                    "type": "string",
                    "description": (
                        "Restrict results to one project's notes."
                    ),
                },
                "source_type": {
                    "type": "string",
                    "description": (
                        "Restrict by note type — persona, project, "
                        "world, standing-instructions, ingested, etc."
                    ),
                },
                "min_score": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": (
                        "Cosine similarity floor; default no floor."
                    ),
                },
            },
            "required": ["query"],
        },
        risk=RiskClass.READ,
        handler=handler,
    )


def _to_dict(h: SearchHit) -> dict[str, Any]:
    return {
        "brain_path": h.brain_path,
        "chunk_idx": h.chunk_idx,
        "heading": h.heading,
        "content": h.content,
        "project_slug": h.project_slug,
        "source_type": h.source_type,
        "score": h.score,
    }


def _render_text(hits: list[SearchHit]) -> str:
    if not hits:
        return "0 brain notes match the query."
    lines = [f"{len(hits)} brain note hit(s):"]
    for h in hits:
        scope = (
            f"project={h.project_slug}"
            if h.project_slug
            else f"source={h.source_type}"
        )
        heading = h.heading or "(no heading)"
        snippet = h.content.strip().splitlines()
        first = snippet[0] if snippet else ""
        if len(first) > 200:
            first = first[:197] + "..."
        lines.append(
            f"- {h.brain_path}#{h.chunk_idx} [{scope}] "
            f"score={h.score:.3f}\n"
            f"   heading: {heading}\n"
            f"   snippet: {first}"
        )
    return "\n".join(lines)


__all__ = ["make_brain_semantic_search_tool"]
