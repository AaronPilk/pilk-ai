"""Telegram notification tools — PILK and every agent can push
messages to the operator without waiting for the operator to
initiate.

Two tools:

    telegram_notify    READ    send a text message to the operator
    telegram_deliver   READ    attach a workspace file (PDF, CSV, …)

Both are READ risk. Telegram is Pilk's ambient channel to the
operator — status updates, approval prompts, incidents — and
gating every ping behind an approval defeats the whole mechanism
(the operator shouldn't have to approve the approval). The tools
resolve chat_id from settings, so there's no way for either to
reach anyone but the operator themselves.

Outbound comms to third parties (email_send_as_me, etc.) stay at
COMMS — those are the ones that still need an approval gate.
"""

from __future__ import annotations

from pathlib import Path

from core.config import get_settings
from core.integrations.telegram import (
    TelegramClient,
    TelegramConfig,
    TelegramError,
)
from core.logging import get_logger
from core.policy.quiet_hours import is_quiet
from core.policy.risk import RiskClass
from core.secrets import resolve_secret
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.tools.telegram")


def _client() -> TelegramClient | tuple[None, str]:
    s = get_settings()
    token = resolve_secret("telegram_bot_token", s.telegram_bot_token)
    chat_id = resolve_secret("telegram_chat_id", s.telegram_chat_id)
    if not token or not chat_id:
        return (
            None,
            "Telegram not configured. Add telegram_bot_token + "
            "telegram_chat_id in Settings → API Keys. Create the bot "
            "via @BotFather, then grab the chat_id by messaging the "
            "bot once and visiting "
            "https://api.telegram.org/bot<token>/getUpdates.",
        )
    return TelegramClient(TelegramConfig(bot_token=token, chat_id=chat_id))


def _unwrap(client_or_err):
    if isinstance(client_or_err, tuple):
        return None, ToolOutcome(content=client_or_err[1], is_error=True)
    return client_or_err, None


def _surface(e: TelegramError) -> ToolOutcome:
    return ToolOutcome(
        content=f"Telegram {e.status}: {e.message}",
        is_error=True,
        data={"status": e.status, "raw": e.raw},
    )


def _workspace_root(ctx: ToolContext) -> Path:
    return (
        ctx.sandbox_root.expanduser().resolve()
        if ctx.sandbox_root is not None
        else get_settings().workspace_dir.expanduser().resolve()
    )


# ── telegram_notify ─────────────────────────────────────────────


async def _notify(args: dict, _ctx: ToolContext) -> ToolOutcome:
    text = str(args.get("text") or "").strip()
    if not text:
        return ToolOutcome(
            content="telegram_notify requires 'text'.",
            is_error=True,
        )
    # Quiet-hours gate on unsolicited proactive pings. Replies to
    # operator-initiated messages never reach this path (the Telegram
    # bridge calls the client directly), so we're only filtering
    # proactive outreach here. Agents with a genuinely urgent reason
    # (sentinel incident, payment failure, etc.) can override with
    # urgent=true; the default is honour quiet hours.
    urgent = bool(args.get("urgent", False))
    if not urgent and is_quiet():
        log.info(
            "telegram_notify_suppressed_quiet_hours",
            text_preview=text[:60],
        )
        return ToolOutcome(
            content=(
                "Suppressed: operator is in quiet hours. This ping was "
                "not sent. Call telegram_notify again with urgent=true "
                "ONLY if this is the kind of thing the operator would "
                "want waking them up for (sentinel incident, financial "
                "failure, stuck approval on a live deadline). Otherwise "
                "save the message for morning or append it to today's "
                "daily note instead."
            ),
            data={"suppressed": True, "reason": "quiet_hours"},
        )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        result = await client.send_message(
            text,
            parse_mode=args.get("parse_mode"),
            disable_web_page_preview=bool(
                args.get("disable_web_page_preview", True)
            ),
        )
    except TelegramError as e:
        return _surface(e)
    message_id = result.get("message_id")
    return ToolOutcome(
        content=(
            f"Sent Telegram message (id={message_id}) "
            f"to chat {result.get('chat', {}).get('id')}."
        ),
        data={"message_id": message_id, "raw": result},
    )


telegram_notify_tool = Tool(
    name="telegram_notify",
    description=(
        "Push a short text message to the operator's Telegram chat. "
        "Use this when an agent (or PILK itself) needs to get the "
        "operator's attention without waiting for the next chat turn "
        "— approval needed, long-running task finished, sentinel "
        "incident, ad-campaign report ready. READ-risk — the "
        "destination is hardwired to the operator's own chat, so no "
        "approval gate. Messages over 4096 chars are truncated; use "
        "telegram_deliver for long content.\n\n"
        "QUIET HOURS: by default this tool suppresses sends during "
        "the operator's configured quiet-hours window (``Settings → "
        "quiet_hours_local``). The default window is 22:00-08:00 "
        "local. Pass urgent=true ONLY when the operator would genuinely "
        "want waking up (sentinel incident, financial emergency, a "
        "live-deadline approval). Routine check-ins, opportunity "
        "nudges, \"job done\" pings — leave urgent=false; during "
        "quiet hours the tool returns suppressed=true and the agent "
        "should log the message to a daily note instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Body of the message. Keep it tight — "
                               "Telegram is a push channel, not a "
                               "dumping ground.",
            },
            "parse_mode": {
                "type": "string",
                "enum": ["MarkdownV2", "HTML"],
                "description": (
                    "Optional Telegram formatting mode. MarkdownV2 "
                    "requires escaping special characters; leave "
                    "unset for plain text."
                ),
            },
            "disable_web_page_preview": {"type": "boolean"},
            "urgent": {
                "type": "boolean",
                "description": (
                    "Bypass the operator's quiet-hours window. Only "
                    "set to true when the operator would want waking "
                    "up — sentinel incidents, financial failures, "
                    "live-deadline approvals. Routine pings leave "
                    "this false (the default) so the tool silently "
                    "defers during quiet hours."
                ),
            },
        },
        "required": ["text"],
    },
    risk=RiskClass.READ,
    handler=_notify,
)


# ── telegram_deliver ────────────────────────────────────────────


async def _deliver(args: dict, ctx: ToolContext) -> ToolOutcome:
    rel = str(args.get("path") or "").strip()
    caption = str(args.get("caption") or "").strip() or None
    if not rel:
        return ToolOutcome(
            content="telegram_deliver requires 'path' (workspace-"
                    "relative).",
            is_error=True,
        )
    root = _workspace_root(ctx)
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return ToolOutcome(
            content=f"path escapes workspace: {rel}", is_error=True,
        )
    if not candidate.is_file():
        return ToolOutcome(
            content=f"not found: {rel}", is_error=True,
        )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        result = await client.send_document(
            candidate, caption=caption,
        )
    except TelegramError as e:
        return _surface(e)
    return ToolOutcome(
        content=(
            f"Delivered {rel} to Telegram (message_id="
            f"{result.get('message_id')})."
        ),
        data={
            "message_id": result.get("message_id"),
            "path": rel,
            "raw": result,
        },
    )


telegram_deliver_tool = Tool(
    name="telegram_deliver",
    description=(
        "Attach a workspace file (PDF report, CSV shortlist, rendered "
        "design) and push it to the operator's Telegram chat with an "
        "optional caption. Path is workspace-relative; the tool "
        "enforces scope. READ-risk — the destination is the operator's "
        "own chat, so no approval gate."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Workspace-relative path to the file to send. "
                    "Must resolve inside the sandbox workspace."
                ),
            },
            "caption": {
                "type": "string",
                "description": (
                    "Optional short caption shown with the file. "
                    "Telegram caps at 1024 chars; the tool truncates."
                ),
            },
        },
        "required": ["path"],
    },
    risk=RiskClass.READ,
    handler=_deliver,
)


TELEGRAM_TOOLS: list[Tool] = [telegram_notify_tool, telegram_deliver_tool]
