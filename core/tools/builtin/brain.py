"""brain_* tools — PILK's long-term Obsidian-compatible note store.

Four operations exposed to the model as separate tools so Claude can
reason about each one independently (and so auto-approval rules apply
cleanly):

    brain_note_read  (READ)         → fetch the contents of a note
    brain_note_write (WRITE_LOCAL)  → create or overwrite a note
    brain_search     (READ)         → substring search across the vault
    brain_note_list  (READ)         → enumerate notes (optionally under
                                      a folder)

Each handler catches vault-level errors and returns them as clean
``is_error`` outcomes so the agent loop can recover without raising.
"""

from __future__ import annotations

from typing import Any

from core.brain import Vault
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

MAX_LIST_SHOWN = 200
MAX_CONTENT_ECHO = 3000  # when the model reads a note, clamp what we echo


def make_brain_tools(vault: Vault) -> list[Tool]:
    return [
        _read_tool(vault),
        _write_tool(vault),
        _search_tool(vault),
        _list_tool(vault),
    ]


def _read_tool(vault: Vault) -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext
    ) -> ToolOutcome:
        path = str(args.get("path") or "").strip()
        if not path:
            return ToolOutcome(
                content="brain_note_read requires a 'path' (vault-relative).",
                is_error=True,
            )
        try:
            body = vault.read(path)
        except FileNotFoundError as e:
            return ToolOutcome(content=str(e), is_error=True)
        except (IsADirectoryError, ValueError) as e:
            return ToolOutcome(content=str(e), is_error=True)
        shown = body[:MAX_CONTENT_ECHO]
        suffix = (
            f"\n\n[truncated — {len(body)} chars, shown {MAX_CONTENT_ECHO}]"
            if len(body) > MAX_CONTENT_ECHO
            else ""
        )
        return ToolOutcome(
            content=f"{path}\n\n{shown}{suffix}",
            data={"path": path, "chars": len(body)},
        )

    return Tool(
        name="brain_note_read",
        description=(
            "Read one markdown note from PILK's long-term brain vault. "
            "Path is relative to the vault root; a trailing `.md` is "
            "added if missing. Returns the note body (truncated to "
            f"{MAX_CONTENT_ECHO} chars in the tool output)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Vault-relative path, e.g. 'clients/skyway/offer-A.md' "
                        "or just 'north-star'."
                    ),
                }
            },
            "required": ["path"],
        },
        risk=RiskClass.READ,
        handler=handler,
    )


def _write_tool(vault: Vault) -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext
    ) -> ToolOutcome:
        path = str(args.get("path") or "").strip()
        content = args.get("content")
        append = bool(args.get("append"))
        if not path:
            return ToolOutcome(
                content="brain_note_write requires a 'path'.",
                is_error=True,
            )
        if not isinstance(content, str) or not content.strip():
            return ToolOutcome(
                content="brain_note_write requires non-empty 'content'.",
                is_error=True,
            )
        try:
            abs_path = vault.write(path, content, append=append)
        except ValueError as e:
            return ToolOutcome(content=str(e), is_error=True)
        except OSError as e:
            return ToolOutcome(
                content=f"write failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        verb = "appended to" if append else "saved"
        return ToolOutcome(
            content=(
                f"{verb} {path} ({len(content)} chars). "
                f"Opens in Obsidian as-is."
            ),
            data={
                "path": path,
                "absolute_path": str(abs_path),
                "chars": len(content),
                "append": append,
            },
        )

    return Tool(
        name="brain_note_write",
        description=(
            "Create or overwrite a markdown note in the brain vault. "
            "Use append=true to add to the end of an existing note "
            "(separated by a blank line). Use wikilinks [[Other Note]] "
            "to cross-reference — Obsidian picks those up automatically. "
            "One topic per note; keep titles readable."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Vault-relative path. Nested folders are created "
                        "automatically (e.g. 'clients/skyway/offer-A.md')."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Full markdown body of the note.",
                },
                "append": {
                    "type": "boolean",
                    "description": (
                        "If true, append to the existing note instead of "
                        "overwriting. Default false."
                    ),
                },
            },
            "required": ["path", "content"],
        },
        risk=RiskClass.WRITE_LOCAL,
        handler=handler,
    )


def _search_tool(vault: Vault) -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext
    ) -> ToolOutcome:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolOutcome(
                content="brain_search requires a non-empty 'query'.",
                is_error=True,
            )
        limit = int(args.get("limit") or 20)
        limit = max(1, min(limit, 100))
        hits = vault.search(query, limit=limit)
        if not hits:
            return ToolOutcome(
                content=f"No hits for '{query}'.",
                data={"query": query, "hits": []},
            )
        lines = [f"Found {len(hits)} hit(s) for '{query}':"]
        for h in hits:
            lines.append(f"- {h.path}:{h.line} — {h.snippet}")
        return ToolOutcome(
            content="\n".join(lines),
            data={
                "query": query,
                "hits": [
                    {"path": h.path, "line": h.line, "snippet": h.snippet}
                    for h in hits
                ],
            },
        )

    return Tool(
        name="brain_search",
        description=(
            "Case-insensitive substring search across every markdown "
            "note in the brain vault. Returns up to `limit` hits with "
            "path:line plus a short snippet. Use this before writing "
            "a new note to avoid duplicating what you already know."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Max hits to return (default 20).",
                },
            },
            "required": ["query"],
        },
        risk=RiskClass.READ,
        handler=handler,
    )


def _list_tool(vault: Vault) -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext
    ) -> ToolOutcome:
        folder = args.get("folder")
        folder_str = (
            str(folder).strip()
            if isinstance(folder, str) and folder.strip()
            else None
        )
        try:
            paths = vault.list(folder_str)
        except ValueError as e:
            return ToolOutcome(content=str(e), is_error=True)
        truncated = len(paths) > MAX_LIST_SHOWN
        shown = paths[:MAX_LIST_SHOWN]
        header = (
            f"{len(paths)} note(s) in "
            f"{folder_str or 'vault root'}"
        )
        if not paths:
            return ToolOutcome(
                content=f"{header}. Vault is empty — use brain_note_write to create one.",
                data={"folder": folder_str, "paths": []},
            )
        body = "\n".join(f"- {p}" for p in shown)
        suffix = (
            f"\n\n[showing first {MAX_LIST_SHOWN} of {len(paths)}]"
            if truncated
            else ""
        )
        return ToolOutcome(
            content=f"{header}:\n{body}{suffix}",
            data={"folder": folder_str, "paths": shown, "total": len(paths)},
        )

    return Tool(
        name="brain_note_list",
        description=(
            "List markdown notes in the brain vault. Pass `folder` to "
            "scope the listing (e.g. 'clients/' or 'daily-notes/'); "
            "omit for the whole vault. Paths returned are vault-"
            "relative."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": (
                        "Optional vault-relative folder path. Omit to "
                        "list everything."
                    ),
                }
            },
        },
        risk=RiskClass.READ,
        handler=handler,
    )
