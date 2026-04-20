"""memory_remember — let PILK write a memory entry on the operator's behalf.

Built primarily for the "Talk to PILK" onboarding interview flow.
During that flow the model asks a question, waits for an answer, and
every few answers calls this tool to durably save a distilled fact,
preference, standing instruction, or pattern.

Risk posture: WRITE_LOCAL. The store is local SQLite; nothing leaves
the machine. A careless write costs nothing beyond adding a line the
operator can delete from ``/memory``. That matches the
``/memory`` API's posture (auto-allowed at the Assistant autonomy
profile) — no approval prompts mid-interview would defeat the
conversational flow.
"""

from __future__ import annotations

from typing import Any

from core.memory import MemoryStore
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

VALID_KINDS = ("preference", "standing_instruction", "fact", "pattern")
MAX_TITLE_CHARS = 140
MAX_BODY_CHARS = 2000


def make_memory_remember_tool(memory: MemoryStore) -> Tool:
    async def handler(
        args: dict[str, Any], ctx: ToolContext
    ) -> ToolOutcome:
        kind = str(args.get("kind") or "").strip().lower()
        title = str(args.get("title") or "").strip()
        body = str(args.get("body") or "").strip()

        if kind not in VALID_KINDS:
            return ToolOutcome(
                content=(
                    f"memory_remember requires 'kind' to be one of "
                    f"{list(VALID_KINDS)}, got {kind!r}."
                ),
                is_error=True,
            )
        if not title:
            return ToolOutcome(
                content="memory_remember requires a non-empty 'title'.",
                is_error=True,
            )
        if len(title) > MAX_TITLE_CHARS:
            return ToolOutcome(
                content=(
                    f"title too long ({len(title)} chars); cap is "
                    f"{MAX_TITLE_CHARS}. Shorten the headline and move "
                    "detail into body."
                ),
                is_error=True,
            )
        if len(body) > MAX_BODY_CHARS:
            return ToolOutcome(
                content=(
                    f"body too long ({len(body)} chars); cap is "
                    f"{MAX_BODY_CHARS}. Save the highlights and skip "
                    "verbatim transcripts."
                ),
                is_error=True,
            )

        try:
            entry = await memory.add(
                kind=kind,
                title=title,
                body=body,
                source="pilk",
                plan_id=ctx.plan_id,
            )
        except ValueError as e:
            return ToolOutcome(content=str(e), is_error=True)

        return ToolOutcome(
            content=(
                f"Saved {kind} to memory: \"{entry.title}\" (id={entry.id})."
            ),
            data={
                "id": entry.id,
                "kind": entry.kind,
                "title": entry.title,
                "body": entry.body,
            },
        )

    return Tool(
        name="memory_remember",
        description=(
            "Save a short entry into PILK's long-term memory on behalf "
            "of the operator. Use during a 'get to know me' interview "
            "(or any time the operator states a preference, rule, fact, "
            "or recurring pattern). Four kinds: 'preference' for soft "
            "likes/dislikes, 'standing_instruction' for rules PILK "
            "should always follow, 'fact' for remembered info like "
            "names + birthdays, 'pattern' for recurring workflows. "
            "Title ≤ 140 chars, body ≤ 2000. Do not paste raw transcript "
            "into body — distil."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(VALID_KINDS),
                    "description": "Which memory bucket the entry belongs to.",
                },
                "title": {
                    "type": "string",
                    "description": (
                        "One-line human-readable headline — the thing the "
                        "operator will scan in /memory."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Optional supporting detail or context. Keep it "
                        "tight; prose not transcript."
                    ),
                },
            },
            "required": ["kind", "title"],
        },
        risk=RiskClass.WRITE_LOCAL,
        handler=handler,
    )
