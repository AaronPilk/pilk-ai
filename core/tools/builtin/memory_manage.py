"""memory_list + memory_delete — read + prune the memory store.

Companion to ``memory_remember``. Together they let the orchestrator
(or an agent) run the full CRUD cycle on the structured memory store
without bouncing the operator to the dashboard:

- ``memory_list`` enumerates entries, optionally filtered by kind.
- ``memory_delete`` drops one entry by id or by exact title.

Both tools share ``memory_remember``'s posture: WRITE_LOCAL risk on
delete (matches the store's /memory DELETE route), READ on list. The
store is SQLite on the local machine; no network path.
"""

from __future__ import annotations

from typing import Any

from core.memory import MemoryStore
from core.memory.store import VALID_KINDS
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

# Pagination cap for the tool output. The UI reads /memory directly;
# this tool is for the model's own recall pass, where 50 entries is
# already more context than it needs.
MAX_LIST_RETURN = 50


def make_memory_list_tool(memory: MemoryStore) -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext
    ) -> ToolOutcome:
        raw_kind = args.get("kind")
        kind = (
            str(raw_kind).strip().lower()
            if isinstance(raw_kind, str) and raw_kind.strip()
            else None
        )
        if kind is not None and kind not in VALID_KINDS:
            return ToolOutcome(
                content=(
                    f"memory_list 'kind' must be one of "
                    f"{sorted(VALID_KINDS)}, got {kind!r}."
                ),
                is_error=True,
            )
        try:
            entries = await memory.list(kind=kind)
        except ValueError as e:
            return ToolOutcome(content=str(e), is_error=True)
        truncated = len(entries) > MAX_LIST_RETURN
        shown = entries[:MAX_LIST_RETURN]
        header = (
            f"{len(entries)} memory entrie(s)"
            + (f" of kind {kind}" if kind else "")
            + ":"
        )
        if not entries:
            return ToolOutcome(
                content=(
                    f"No memory entries{' of kind ' + kind if kind else ''}. "
                    "Use memory_remember to add one."
                ),
                data={"kind": kind, "entries": []},
            )
        lines = [
            f"- [{e.kind}] {e.title}  (id={e.id})"
            + (f"\n    {e.body[:200]}" if e.body else "")
            for e in shown
        ]
        if truncated:
            lines.append(f"\n[showing first {MAX_LIST_RETURN} of {len(entries)}]")
        return ToolOutcome(
            content=header + "\n" + "\n".join(lines),
            data={
                "kind": kind,
                "entries": [e.public_dict() for e in shown],
                "total": len(entries),
            },
        )

    return Tool(
        name="memory_list",
        description=(
            "List PILK's saved memory entries. Pass `kind` to filter "
            "(preference / standing_instruction / fact / pattern). Use "
            "this when the operator asks 'what do you know about me?' "
            "or when you need to recall a specific preference before "
            "answering. Returns up to 50 entries with id, kind, title, "
            "and a body preview."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": sorted(VALID_KINDS),
                    "description": (
                        "Optional filter. Omit to list every kind."
                    ),
                },
            },
        },
        risk=RiskClass.READ,
        handler=handler,
    )


def make_memory_delete_tool(memory: MemoryStore) -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext
    ) -> ToolOutcome:
        raw_id = args.get("id")
        raw_title = args.get("title")
        entry_id = (
            str(raw_id).strip()
            if isinstance(raw_id, str) and raw_id.strip()
            else None
        )
        title = (
            str(raw_title).strip()
            if isinstance(raw_title, str) and raw_title.strip()
            else None
        )
        if entry_id is None and title is None:
            return ToolOutcome(
                content=(
                    "memory_delete requires either 'id' or 'title' "
                    "(exact match). Use memory_list first if you're "
                    "unsure of the id."
                ),
                is_error=True,
            )

        # Resolve title → id. We list everything (there aren't enough
        # entries for this to matter) and pick the most recent exact
        # title match. Two entries with identical titles are rare, but
        # we use "most recent" as the tiebreak rather than refusing,
        # since it matches what the operator likely wants.
        if entry_id is None:
            all_entries = await memory.list()
            matches = [e for e in all_entries if e.title == title]
            if not matches:
                return ToolOutcome(
                    content=(
                        f"No memory entry with exact title {title!r}. "
                        "Try memory_list to find the right one."
                    ),
                    is_error=True,
                )
            entry_id = matches[0].id  # list() orders DESC by created_at

        removed = await memory.delete(entry_id)
        if not removed:
            return ToolOutcome(
                content=f"No memory entry with id {entry_id!r}.",
                is_error=True,
            )
        return ToolOutcome(
            content=f"Deleted memory entry {entry_id}.",
            data={"id": entry_id, "deleted": True},
        )

    return Tool(
        name="memory_delete",
        description=(
            "Remove one memory entry by id or by exact title. Use when "
            "the operator says 'forget that' or corrects a stored fact. "
            "Prefer `id` (exact); `title` match is a convenience when "
            "you only have the headline. Irreversible — the row is "
            "dropped from SQLite. WRITE_LOCAL risk."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": (
                        "Memory entry id (e.g. 'mem_abc123...'). "
                        "Preferred — exact and unambiguous."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Alternative to id: exact title match. On "
                        "duplicates, the most recent entry is removed."
                    ),
                },
            },
        },
        risk=RiskClass.WRITE_LOCAL,
        handler=handler,
    )


__all__ = ["make_memory_delete_tool", "make_memory_list_tool"]
