"""Brain-ingest tools — pull content from outside the vault into it.

Three tools cover the V1 ingestion surface:

    brain_ingest_claude_code    READ + WRITE_LOCAL    ~/.claude/projects/
    brain_ingest_chatgpt        READ + WRITE_LOCAL    workspace zip/json
    brain_ingest_gmail          NET_READ + WRITE_LOCAL  user's Gmail inbox

Each reads from a specific, well-known location (not arbitrary paths):

- Claude Code: ``~/.claude/projects/`` (set by the CLI, not us).
- ChatGPT: an operator-supplied zip path inside the workspace.
- Gmail: the operator's own user-role Google OAuth, scoped to their
  inbox. Same account the inbox_triage_agent reads.

Each writes normalised markdown to the brain vault under
``ingested/<source>/``. Re-running is idempotent — the note at the
derived path is overwritten with the latest rendering, so the vault
always reflects the current state of the source.

Risk model:

* Claude Code + ChatGPT reads are benign — paths are the operator's
  own artefacts.
* Gmail reads hit the network, so risk class is NET_READ.
* All writes go to the brain vault = WRITE_LOCAL.

We deliberately DON'T classify these as IRREVERSIBLE just because
they touch $HOME. The scope is narrow (one specific subdirectory per
ingester), the content is the operator's own chat/email logs, and
the output goes straight to their brain vault. Treating these like
file-system-wide reads would push every brain-load into the approval
queue for no real safety gain.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.brain import Vault
from core.config import get_settings
from core.integrations.ingesters.apple_messages import (
    DEFAULT_SINCE_DAYS as MESSAGES_DEFAULT_SINCE_DAYS,
)
from core.integrations.ingesters.apple_messages import (
    HARD_MAX_THREADS as MESSAGES_HARD_MAX_THREADS,
)
from core.integrations.ingesters.apple_messages import (
    AppleMessagesIngestError,
    scan_apple_messages,
)
from core.integrations.ingesters.apple_messages import (
    render_thread_note as render_messages_thread_note,
)
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
from core.integrations.ingesters.docs import (
    DEFAULT_EXTENSIONS as DOCS_DEFAULT_EXTENSIONS,
)
from core.integrations.ingesters.docs import (
    DEFAULT_MAX_FILES as DOCS_DEFAULT_MAX_FILES,
)
from core.integrations.ingesters.docs import (
    HARD_MAX_FILES as DOCS_HARD_MAX_FILES,
)
from core.integrations.ingesters.docs import (
    DocsIngestError,
    render_doc_note,
    scan_docs,
)
from core.integrations.ingesters.gmail import (
    DEFAULT_MAX_THREADS,
    DEFAULT_QUERY,
    render_thread_note,
    scan_threads,
)
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

if TYPE_CHECKING:
    from core.identity import AccountsStore

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
            "the note at the derived path. "
            "IMPORTANT: if the operator says they already uploaded "
            "their ChatGPT export through the dashboard's 'Upload to "
            "Brain' flow, the conversations are already in "
            "`ingested/chatgpt/` as per-conversation notes — search "
            "them with `brain_search` / `brain_note_read` rather than "
            "asking for a zip path."
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


def _gmail_ingest_tool(
    vault: Vault, accounts: AccountsStore | None,
) -> Tool:
    async def _handler(args: dict, _ctx: ToolContext) -> ToolOutcome:
        if accounts is None:
            return ToolOutcome(
                content=(
                    "brain_ingest_gmail requires the Google account store. "
                    "Daemon booted without identity wiring — this is a "
                    "PILK config problem, not a user fix."
                ),
                is_error=True,
            )
        creds, account = _load_user_gmail_creds(accounts)
        if creds is None:
            return ToolOutcome(
                content=(
                    "Your Google account isn't connected as the 'user' "
                    "role yet. Open Settings → Connected accounts and "
                    "link your personal Google account with Gmail "
                    "read-only scope. The inbox_triage_agent uses the "
                    "same binding — linking once unlocks both."
                ),
                is_error=True,
            )
        query = str(args.get("query") or DEFAULT_QUERY).strip() or DEFAULT_QUERY
        try:
            max_threads = int(args.get("max_threads") or DEFAULT_MAX_THREADS)
        except (TypeError, ValueError):
            max_threads = DEFAULT_MAX_THREADS
        max_threads = max(1, min(max_threads, 500))
        try:
            import asyncio
            threads = await asyncio.to_thread(
                scan_threads, creds, query=query, max_threads=max_threads,
            )
        except Exception as e:
            log.exception("brain_ingest_gmail_scan_failed", error=str(e))
            return ToolOutcome(
                content=f"Gmail scan failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        if not threads:
            return ToolOutcome(
                content=(
                    f"Gmail query {query!r} matched zero threads. "
                    "Try a wider filter (e.g. 'newer_than:90d')."
                ),
                data={"query": query, "threads": 0, "written": []},
            )
        written: list[dict] = []
        errors: list[str] = []
        for thread in threads:
            note = render_thread_note(thread)
            try:
                abs_path = vault.write(note.path, note.body)
            except (OSError, ValueError) as e:
                errors.append(f"{thread.thread_id}: {e}")
                continue
            written.append(
                {
                    "path": abs_path.relative_to(vault.root).as_posix(),
                    "thread_id": thread.thread_id,
                    "subject": thread.subject,
                    "messages": len(thread.messages),
                }
            )
        summary = (
            f"Ingested {len(written)}/{len(threads)} Gmail thread(s) "
            f"from account {account.email if account else '?'} into "
            "`ingested/gmail/`."
        )
        if errors:
            summary += f" {len(errors)} failure(s): {errors[:3]}"
        return ToolOutcome(
            content=summary,
            data={
                "query": query,
                "threads_scanned": len(threads),
                "written": written,
                "errors": errors,
            },
        )

    return Tool(
        name="brain_ingest_gmail",
        description=(
            "Pull the operator's Gmail inbox into the brain vault. "
            "One markdown note per thread under `ingested/gmail/` "
            "named `YYYY-MM-DD-<subject-slug>.md`. Reuses the same "
            "user-role Google OAuth the inbox_triage_agent holds. "
            "Default query is `newer_than:30d`; pass a narrower or "
            "broader Gmail search (e.g. `from:aaron@skyway.media`, "
            "`is:starred`, `newer_than:6m`) to target specific slices."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Gmail search operators. Default "
                        f"`{DEFAULT_QUERY}`."
                    ),
                },
                "max_threads": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": (
                        "Cap on threads per run (default "
                        f"{DEFAULT_MAX_THREADS})."
                    ),
                },
            },
        },
        risk=RiskClass.NET_READ,
        handler=_handler,
    )


def _load_user_gmail_creds(
    accounts: AccountsStore,
) -> tuple[Any, Any]:
    """Resolve + decrypt the user-role Google token. Mirrors the
    private helper inside make_gmail_tools so we don't have to plumb
    a second creds-loader through the ingester."""
    from core.identity.accounts import AccountBinding
    from core.integrations.google.oauth import credentials_from_blob

    binding = AccountBinding(provider="google", role="user")
    account = accounts.resolve_binding(binding)
    if account is None:
        return None, None
    tokens = accounts.load_tokens(account.account_id)
    if tokens is None:
        return None, account
    blob = {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "client_id": tokens.client_id,
        "client_secret": tokens.client_secret,
        "scopes": tokens.scopes,
        "token_uri": tokens.token_uri,
        "email": account.email,
    }
    return credentials_from_blob(blob), account


def _docs_ingest_tool(vault: Vault) -> Tool:
    """Walk a home-scoped folder of plain-text docs and stage each as
    a markdown note under ``ingested/docs/``. Original folder
    structure is preserved so the Obsidian graph reflects the source
    layout.

    Gated at the daemon level by ``COMPUTER_CONTROL_ENABLED`` — the
    ingester itself refuses any path outside ``$HOME`` regardless of
    the kill-switch, as a defence-in-depth layer.
    """

    async def _handler(args: dict, _ctx: ToolContext) -> ToolOutcome:
        raw_source = str(args.get("source_dir") or "").strip()
        if not raw_source:
            return ToolOutcome(
                content=(
                    "brain_ingest_docs requires 'source_dir' — a folder "
                    "path under your home directory."
                ),
                is_error=True,
            )
        try:
            max_files = int(args.get("max_files") or DOCS_DEFAULT_MAX_FILES)
        except (TypeError, ValueError):
            max_files = DOCS_DEFAULT_MAX_FILES
        max_files = max(1, min(max_files, DOCS_HARD_MAX_FILES))
        recursive = bool(args.get("recursive", True))
        exts_raw = args.get("extensions")
        if isinstance(exts_raw, list) and exts_raw:
            extensions = tuple(
                e if e.startswith(".") else f".{e}"
                for e in (str(x).lower() for x in exts_raw)
            )
        else:
            extensions = DOCS_DEFAULT_EXTENSIONS

        try:
            scan = scan_docs(
                Path(raw_source),
                extensions=extensions,
                max_files=max_files,
                recursive=recursive,
            )
        except DocsIngestError as e:
            return ToolOutcome(content=str(e), is_error=True)
        except OSError as e:
            return ToolOutcome(
                content=f"brain_ingest_docs failed to walk source: {e}",
                is_error=True,
            )

        written: list[dict] = []
        errors: list[str] = []
        for doc in scan.found:
            note = render_doc_note(doc, scan_root=scan.root)
            try:
                rel = _write_note(vault, note.path, note.body)
            except (OSError, ValueError) as e:
                errors.append(f"{doc.rel_path.as_posix()}: {e}")
                continue
            written.append(
                {
                    "path": rel,
                    "source": str(doc.abs_path),
                    "title": note.title,
                    "size": doc.size,
                }
            )

        summary = (
            f"Ingested {len(written)}/{len(scan.found)} doc(s) from "
            f"`{scan.root}` into `ingested/docs/`. Skipped "
            f"{len(scan.skipped)} non-matching / oversized / "
            "undecodable file(s)."
        )
        if errors:
            summary += f" {len(errors)} write failure(s): {errors[:3]}"
        return ToolOutcome(
            content=summary,
            data={
                "source_dir": str(scan.root),
                "written": written,
                "skipped_count": len(scan.skipped),
                "errors": errors,
            },
        )

    return Tool(
        name="brain_ingest_docs",
        description=(
            "Walk an operator-chosen folder of plain-text documents "
            "(`.md`, `.txt`, `.rtf`, `.html`, `.log`, `.csv`, `.json`, "
            "`.yaml`, …) and stage every readable file as a markdown "
            "note under `ingested/docs/`. Preserves the source folder "
            "layout so the Obsidian graph clusters by origin. "
            "`source_dir` must live under the operator's home "
            "directory; binary / oversized / non-matching files are "
            "skipped silently. Gated by COMPUTER_CONTROL_ENABLED at "
            "the daemon level."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "source_dir": {
                    "type": "string",
                    "description": (
                        "Absolute or ~-relative path under your home "
                        "directory (e.g. '~/Documents/projects')."
                    ),
                },
                "recursive": {
                    "type": "boolean",
                    "description": (
                        "Walk subdirectories (default true). Set false "
                        "to ingest only the top-level files."
                    ),
                },
                "max_files": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": DOCS_HARD_MAX_FILES,
                    "description": (
                        f"Cap on ingested files (default "
                        f"{DOCS_DEFAULT_MAX_FILES}, max "
                        f"{DOCS_HARD_MAX_FILES})."
                    ),
                },
                "extensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Override allowed suffixes. Defaults to the "
                        "standard text-native set."
                    ),
                },
            },
            "required": ["source_dir"],
        },
        # WRITE_LOCAL because the net effect is a vault write; the
        # source read is bounded to the operator's own $HOME and
        # the daemon-level COMPUTER_CONTROL kill-switch still
        # applies to the underlying computer_* surface if present.
        risk=RiskClass.WRITE_LOCAL,
        handler=_handler,
    )


def _apple_messages_ingest_tool(vault: Vault) -> Tool:
    """Pull local Apple Messages history into the vault as per-contact
    markdown notes. Reads ``~/Library/Messages/chat.db`` read-only;
    requires macOS Full Disk Access on the pilkd process.
    """

    async def _handler(args: dict, _ctx: ToolContext) -> ToolOutcome:
        try:
            since_days = int(
                args.get("since_days") or MESSAGES_DEFAULT_SINCE_DAYS
            )
        except (TypeError, ValueError):
            since_days = MESSAGES_DEFAULT_SINCE_DAYS
        include_groups = bool(args.get("include_groups", False))
        skip_shortcodes = bool(args.get("skip_shortcodes", True))
        try:
            max_threads = int(
                args.get("max_threads") or MESSAGES_HARD_MAX_THREADS
            )
        except (TypeError, ValueError):
            max_threads = MESSAGES_HARD_MAX_THREADS

        try:
            scan = scan_apple_messages(
                since_days=since_days,
                include_groups=include_groups,
                skip_shortcodes=skip_shortcodes,
                max_threads=max_threads,
            )
        except AppleMessagesIngestError as e:
            return ToolOutcome(content=str(e), is_error=True)

        written: list[dict] = []
        errors: list[str] = []
        for thread in scan.threads:
            note = render_messages_thread_note(thread)
            try:
                rel = _write_note(vault, note.path, note.body)
            except (OSError, ValueError) as e:
                errors.append(f"{thread.note_slug}: {e}")
                continue
            written.append(
                {
                    "path": rel,
                    "chat_id": thread.chat_id,
                    "title": note.title,
                    "messages": len(thread.messages),
                }
            )

        total_msgs = sum(t["messages"] for t in written)
        summary = (
            f"Ingested {len(written)} conversation(s) ({total_msgs} "
            f"messages) from the last {since_days} day(s) into "
            f"`ingested/messages/`. Skipped {len(scan.skipped)} "
            "non-text / shortcode / out-of-scope chat(s)."
        )
        if errors:
            summary += f" {len(errors)} write failure(s): {errors[:3]}"
        return ToolOutcome(
            content=summary,
            data={
                "since_days": since_days,
                "written": written,
                "skipped_count": len(scan.skipped),
                "errors": errors,
            },
        )

    return Tool(
        name="brain_ingest_messages",
        description=(
            "Pull the operator's local Apple Messages history into the "
            "brain vault. One markdown note per 1:1 conversation under "
            "`ingested/messages/<handle>.md`, messages grouped by day. "
            "Reads `~/Library/Messages/chat.db` read-only; requires "
            "Full Disk Access granted to the pilkd process on macOS. "
            "Defaults to last 90 days of DMs, skipping numeric "
            "shortcodes (verification codes)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "since_days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3650,
                    "description": (
                        "How many days of history to ingest (default "
                        f"{MESSAGES_DEFAULT_SINCE_DAYS})."
                    ),
                },
                "include_groups": {
                    "type": "boolean",
                    "description": (
                        "Include group chats. Default false — group "
                        "chats flood the vault and are rarely worth "
                        "archiving as per-conversation notes."
                    ),
                },
                "skip_shortcodes": {
                    "type": "boolean",
                    "description": (
                        "Skip numeric-only senders like 40404 "
                        "(verification codes, shortcode marketing). "
                        "Default true."
                    ),
                },
                "max_threads": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MESSAGES_HARD_MAX_THREADS,
                    "description": (
                        "Cap on threads per run (default "
                        f"{MESSAGES_HARD_MAX_THREADS})."
                    ),
                },
            },
        },
        risk=RiskClass.WRITE_LOCAL,
        handler=_handler,
    )


def make_brain_ingest_tools(
    vault: Vault, accounts: AccountsStore | None = None,
) -> list[Tool]:
    """Factory: build the ingest tools bound to the given brain vault.

    Called at app startup — mirrors `make_brain_tools` so the brain
    wiring has a single, obvious home. Pass the AccountsStore to
    enable Gmail ingest; omit for a minimal brain setup without
    OAuth."""
    tools: list[Tool] = [
        _claude_code_ingest_tool(vault),
        _chatgpt_ingest_tool(vault),
        _docs_ingest_tool(vault),
        _apple_messages_ingest_tool(vault),
    ]
    tools.append(_gmail_ingest_tool(vault, accounts))
    return tools


__all__ = ["make_brain_ingest_tools"]
