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
# When the model reads a note, clamp what we echo back. Big enough
# that a full Telegram session log (typical: 5-50k chars) shows in
# one read, so PILK can recall the whole conversation when the
# operator asks "what did we talk about earlier?". The old 3k cap
# was sized for short note snippets and silently truncated long
# session journals — operator would ask about a long chat and PILK
# would only see the first 3k, then confabulate the rest.
MAX_CONTENT_ECHO = 60_000


def make_brain_tools(vault: Vault) -> list[Tool]:
    return [
        _read_tool(vault),
        _write_tool(vault),
        _search_tool(vault),
        _list_tool(vault),
        _search_and_replace_tool(vault),
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
            offset = max(0, int(args.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            body = vault.read(path)
        except FileNotFoundError as e:
            return ToolOutcome(content=str(e), is_error=True)
        except (IsADirectoryError, ValueError) as e:
            return ToolOutcome(content=str(e), is_error=True)
        total = len(body)
        if offset >= total and total > 0:
            return ToolOutcome(
                content=(
                    f"{path}\n\n[empty range — note is {total} chars, "
                    f"requested offset {offset}]"
                ),
                data={"path": path, "chars": total, "offset": offset},
            )
        window = body[offset : offset + MAX_CONTENT_ECHO]
        end = offset + len(window)
        if end < total:
            suffix = (
                f"\n\n[partial read — chars {offset}-{end} of {total}; "
                f"call again with offset={end} to continue]"
            )
        elif offset > 0:
            suffix = f"\n\n[end of note — chars {offset}-{end} of {total}]"
        else:
            suffix = ""
        return ToolOutcome(
            content=f"{path}\n\n{window}{suffix}",
            data={
                "path": path,
                "chars": total,
                "offset": offset,
                "next_offset": end if end < total else None,
            },
        )

    return Tool(
        name="brain_note_read",
        description=(
            "Read one markdown note from PILK's long-term brain vault. "
            "Path is relative to the vault root; a trailing `.md` is "
            f"added if missing. Returns up to {MAX_CONTENT_ECHO} chars "
            "per call. For longer notes (multi-hour Telegram session "
            "logs, daily digests), pass `offset` to continue from where "
            "the previous read stopped — the response includes the "
            "next offset to use."
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
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Character offset to start reading from. Default 0 "
                        "(start of file). Use the `next_offset` returned "
                        "by a previous partial read to continue."
                    ),
                },
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


def _search_and_replace_tool(vault: Vault) -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext
    ) -> ToolOutcome:
        path = str(args.get("path") or "").strip()
        find = args.get("find")
        replace = args.get("replace")
        replace_all = args.get("replace_all")
        if replace_all is None:
            replace_all = True  # documented default
        if not path:
            return ToolOutcome(
                content=(
                    "brain_note_search_and_replace requires a 'path'."
                ),
                is_error=True,
            )
        if not isinstance(find, str) or find == "":
            return ToolOutcome(
                content=(
                    "brain_note_search_and_replace requires a non-empty "
                    "'find' string."
                ),
                is_error=True,
            )
        if not isinstance(replace, str):
            return ToolOutcome(
                content=(
                    "brain_note_search_and_replace requires a 'replace' "
                    "string (empty string allowed, e.g. to delete)."
                ),
                is_error=True,
            )
        try:
            body = vault.read(path)
        except FileNotFoundError as e:
            return ToolOutcome(content=str(e), is_error=True)
        except (IsADirectoryError, ValueError) as e:
            return ToolOutcome(content=str(e), is_error=True)
        count_before = body.count(find)
        if count_before == 0:
            return ToolOutcome(
                content=(
                    f"No occurrences of {find!r} in {path}. Nothing to "
                    "replace."
                ),
                data={"path": path, "replaced": 0, "find": find},
                is_error=True,
            )
        if replace_all:
            new_body = body.replace(find, replace)
            replaced = count_before
        else:
            new_body = body.replace(find, replace, 1)
            replaced = 1
        try:
            vault.write(path, new_body, append=False)
        except ValueError as e:
            return ToolOutcome(content=str(e), is_error=True)
        except OSError as e:
            return ToolOutcome(
                content=f"write failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=(
                f"Replaced {replaced} occurrence(s) of {find!r} "
                f"in {path}."
            ),
            data={
                "path": path,
                "replaced": replaced,
                "find": find,
                "replace_all": bool(replace_all),
            },
        )

    return Tool(
        name="brain_note_search_and_replace",
        description=(
            "Read a vault note, substitute every occurrence of `find` "
            "with `replace`, and write it back atomically. Exact "
            "string match — no regex. Use for targeted edits like "
            "fixing a typo or renaming an entity across a long note "
            "without rewriting the whole body. Default replaces every "
            "occurrence; pass replace_all=false for just the first. "
            "Errors out (non-destructively) if the `find` string isn't "
            "present so you don't silently no-op."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Vault-relative path to the note.",
                },
                "find": {
                    "type": "string",
                    "description": (
                        "Exact substring to locate. Case-sensitive."
                    ),
                },
                "replace": {
                    "type": "string",
                    "description": (
                        "Replacement. Empty string deletes the match."
                    ),
                },
                "replace_all": {
                    "type": "boolean",
                    "description": (
                        "If false, replace only the first match. "
                        "Default true."
                    ),
                },
            },
            "required": ["path", "find", "replace"],
        },
        risk=RiskClass.WRITE_LOCAL,
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
        sort = str(args.get("sort") or "path").strip().lower()
        if sort not in {"path", "size", "mtime"}:
            sort = "path"

        from datetime import datetime, timezone

        rows: list[dict[str, Any]] = []
        for p in paths:
            try:
                st = (vault.root / p).stat()
                rows.append(
                    {
                        "path": p,
                        "size": st.st_size,
                        "mtime": datetime.fromtimestamp(
                            st.st_mtime, tz=timezone.utc
                        ).isoformat(timespec="seconds"),
                        "_mtime_raw": st.st_mtime,
                    }
                )
            except OSError:
                rows.append(
                    {"path": p, "size": 0, "mtime": "", "_mtime_raw": 0.0}
                )
        if sort == "size":
            rows.sort(key=lambda r: r["size"], reverse=True)
        elif sort == "mtime":
            rows.sort(key=lambda r: r["_mtime_raw"], reverse=True)
        else:
            rows.sort(key=lambda r: r["path"])

        truncated = len(rows) > MAX_LIST_SHOWN
        shown = rows[:MAX_LIST_SHOWN]
        header = (
            f"{len(rows)} note(s) in "
            f"{folder_str or 'vault root'}"
            f" (sorted by {sort})"
        )
        if not rows:
            return ToolOutcome(
                content=(
                    f"{header}. Vault is empty — use brain_note_write to create one."
                ),
                data={"folder": folder_str, "paths": [], "rows": []},
            )

        def _fmt_size(n: int) -> str:
            if n >= 1024:
                return f"{n / 1024:.1f}KB"
            return f"{n}B"

        body = "\n".join(
            f"- {r['path']} — {_fmt_size(r['size'])} — {r['mtime']} UTC"
            for r in shown
        )
        suffix = (
            f"\n\n[showing first {MAX_LIST_SHOWN} of {len(rows)}]"
            if truncated
            else ""
        )
        return ToolOutcome(
            content=f"{header}:\n{body}{suffix}",
            data={
                "folder": folder_str,
                "paths": [r["path"] for r in shown],
                "rows": [
                    {k: v for k, v in r.items() if not k.startswith("_")}
                    for r in shown
                ],
                "total": len(rows),
            },
        )

    return Tool(
        name="brain_note_list",
        description=(
            "List markdown notes in the brain vault with size + last-"
            "modified time so you can spot the long / recent ones at a "
            "glance. Pass `folder` to scope the listing (e.g. "
            "'sessions/telegram/'); omit for the whole vault. Pass "
            "`sort='size'` to find the biggest conversations (long "
            "Telegram sessions), `sort='mtime'` for most-recent-first. "
            "Paths returned are vault-relative; timestamps are in UTC."
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
                },
                "sort": {
                    "type": "string",
                    "enum": ["path", "size", "mtime"],
                    "description": (
                        "Sort order. 'path' (default) for alphabetical, "
                        "'size' for biggest first (best for finding long "
                        "session logs), 'mtime' for most recent first."
                    ),
                },
            },
        },
        risk=RiskClass.READ,
        handler=handler,
    )
