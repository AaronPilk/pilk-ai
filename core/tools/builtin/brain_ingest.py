"""Brain-ingest tools — pull content from outside the vault into it.

Two tools cover the V1 ingestion surface:

    brain_ingest_claude_code    READ + WRITE_LOCAL
    brain_ingest_chatgpt        READ + WRITE_LOCAL

Each reads from a specific, well-known location (not arbitrary paths):

- Claude Code: ``~/.claude/projects/`` (set by the CLI, not us).
- ChatGPT: an operator-supplied zip path inside the workspace.

Each writes normalised markdown to the brain vault under
``ingested/<source>/``. Re-running is idempotent — the note at the
derived path is overwritten with the latest rendering, so the vault
always reflects the current state of the source.

Risk model:

* Read side is benign — these paths are the operator's own artefacts.
* Write side is WRITE_LOCAL — standard approval flow.

We deliberately DON'T classify these as IRREVERSIBLE just because
they touch $HOME. The scope is narrow (one specific subdirectory per
ingester), the content is the operator's own chat logs, and the
output goes straight to their brain vault. Treating these like
file-system-wide reads would push every brain-load into the
approval queue for no real safety gain.
"""

from __future__ import annotations

from pathlib import Path

from core.brain import Vault
from core.config import get_settings
from core.integrations.ingesters.chatgpt import (
    ChatGPTIngestError,
    parse_export,
    render_conversation_note,
)
from core.integrations.ingesters.claude_code import (
    DEFAULT_ROOT as CLAUDE_DEFAULT_ROOT,
)
from core.integrations.ingesters.claude_code import (
    render_project_note,
    scan_projects,
)
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.tools.brain_ingest")

DEFAULT_MAX_PROJECTS = 50
DEFAULT_MAX_CONVERSATIONS = 500


def _write_note(vault: Vault, path: str, body: str) -> str:
    """Write one ingested note; return the vault-relative path that
    actually landed (post .md normalisation)."""
    abs_path = vault.write(path, body)
    return abs_path.relative_to(vault.root).as_posix()


def _workspace_root(ctx: ToolContext) -> Path:
    return (
        ctx.sandbox_root.expanduser().resolve()
        if ctx.sandbox_root is not None
        else get_settings().workspace_dir.expanduser().resolve()
    )


def _claude_code_ingest_tool(vault: Vault) -> Tool:
    async def _handler(args: dict, _ctx: ToolContext) -> ToolOutcome:
        limit = int(args.get("max_projects") or DEFAULT_MAX_PROJECTS)
        projects = scan_projects(CLAUDE_DEFAULT_ROOT)
        if not projects:
            return ToolOutcome(
                content=(
                    f"No Claude Code projects found under "
                    f"{CLAUDE_DEFAULT_ROOT}. Is the CLI installed + "
                    "have you used it?"
                ),
                data={
                    "root": str(CLAUDE_DEFAULT_ROOT),
                    "projects": 0,
                    "written": [],
                },
            )
        projects = projects[:limit]
        written: list[dict] = []
        errors: list[str] = []
        for p in projects:
            note = render_project_note(p)
            try:
                rel = _write_note(vault, note.path, note.body)
            except (OSError, ValueError) as e:
                errors.append(f"{p.slug}: {e}")
                continue
            written.append(
                {
                    "path": rel,
                    "slug": p.slug,
                    "title": note.title,
                    "sessions": len(p.sessions),
                }
            )
        summary = (
            f"Ingested {len(written)}/{len(projects)} Claude Code "
            f"project(s) into brain vault under `ingested/claude-code/`."
        )
        if errors:
            summary += f" {len(errors)} failure(s): {errors[:3]}"
        return ToolOutcome(
            content=summary,
            data={
                "root": str(CLAUDE_DEFAULT_ROOT),
                "projects_scanned": len(projects),
                "written": written,
                "errors": errors,
            },
        )

    return Tool(
        name="brain_ingest_claude_code",
        description=(
            "Scan ~/.claude/projects/ and land one markdown note per "
            "project in the brain vault under `ingested/claude-code/`. "
            "Each note aggregates every session for that project, "
            "sorted newest-first, with user + assistant turns only "
            "(tool calls summarised but not expanded). Idempotent — "
            "re-running overwrites the existing notes with the "
            "current state of the source."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "max_projects": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": (
                        f"Cap on projects to ingest per run (default "
                        f"{DEFAULT_MAX_PROJECTS}). The scan returns "
                        "projects ordered by last activity, so "
                        "limiting keeps the freshest content."
                    ),
                }
            },
        },
        risk=RiskClass.WRITE_LOCAL,
        handler=_handler,
    )


def _chatgpt_ingest_tool(vault: Vault) -> Tool:
    async def _handler(args: dict, ctx: ToolContext) -> ToolOutcome:
        rel = str(args.get("path") or "").strip()
        if not rel:
            return ToolOutcome(
                content=(
                    "brain_ingest_chatgpt requires 'path' — a "
                    "workspace-relative path to the ChatGPT export "
                    "zip or a conversations.json."
                ),
                is_error=True,
            )
        root = _workspace_root(ctx)
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return ToolOutcome(
                content=f"path escapes workspace: {rel}",
                is_error=True,
            )
        if not candidate.is_file():
            return ToolOutcome(
                content=(
                    f"not found: {rel}. Download your ChatGPT export "
                    "(Settings → Data Controls → Export) and drop the "
                    "zip somewhere under the workspace, then retry."
                ),
                is_error=True,
            )
        try:
            conversations = parse_export(candidate)
        except ChatGPTIngestError as e:
            return ToolOutcome(
                content=f"ChatGPT export error: {e}",
                is_error=True,
            )
        if not conversations:
            return ToolOutcome(
                content=(
                    f"{rel}: export parsed successfully but contained "
                    "zero conversations with non-empty user/assistant "
                    "content."
                ),
                data={"path": rel, "conversations": 0, "written": []},
            )
        limit = int(
            args.get("max_conversations") or DEFAULT_MAX_CONVERSATIONS
        )
        conversations = conversations[:limit]
        written: list[dict] = []
        errors: list[str] = []
        for conv in conversations:
            note = render_conversation_note(conv)
            try:
                written_rel = _write_note(vault, note.path, note.body)
            except (OSError, ValueError) as e:
                errors.append(f"{conv.conversation_id}: {e}")
                continue
            written.append(
                {
                    "path": written_rel,
                    "conversation_id": conv.conversation_id,
                    "title": conv.title,
                    "turns": len(conv.turns),
                }
            )
        summary = (
            f"Ingested {len(written)}/{len(conversations)} ChatGPT "
            f"conversation(s) into brain vault under `ingested/chatgpt/`."
        )
        if errors:
            summary += f" {len(errors)} failure(s): {errors[:3]}"
        return ToolOutcome(
            content=summary,
            data={
                "path": rel,
                "conversations_scanned": len(conversations),
                "written": written,
                "errors": errors,
            },
        )

    return Tool(
        name="brain_ingest_chatgpt",
        description=(
            "Parse a ChatGPT 'Export data' zip (or a raw "
            "conversations.json) and land one markdown note per "
            "conversation in the brain vault under "
            "`ingested/chatgpt/`. Forked branches are dropped; only "
            "the thread the operator actually saw is retained. "
            "Idempotent by conversation id — re-running overwrites "
            "the note at the derived path."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Workspace-relative path to the export zip "
                        "or conversations.json."
                    ),
                },
                "max_conversations": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5000,
                    "description": (
                        "Cap on conversations per run (default "
                        f"{DEFAULT_MAX_CONVERSATIONS}). Conversations "
                        "are ordered newest-first."
                    ),
                },
            },
            "required": ["path"],
        },
        risk=RiskClass.WRITE_LOCAL,
        handler=_handler,
    )


def make_brain_ingest_tools(vault: Vault) -> list[Tool]:
    """Factory: build the ingest tools bound to the given brain vault.

    Called at app startup — mirrors `make_brain_tools` so the brain
    wiring has a single, obvious home."""
    return [
        _claude_code_ingest_tool(vault),
        _chatgpt_ingest_tool(vault),
    ]


__all__ = ["make_brain_ingest_tools"]
