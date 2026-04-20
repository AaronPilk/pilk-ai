"""Telegram notification tools — PILK and every agent can push
messages to the operator without waiting for the operator to
initiate.

Two tools:

    telegram_notify    COMMS    send a text message to the operator
    telegram_deliver   COMMS    attach a workspace file (PDF, CSV, …)

Both are COMMS risk, so every send queues for operator approval by
default — same gate as email. The operator can set a temporary trust
rule ("trust telegram_notify from sentinel for 1h") once comfortable,
but V1 ships conservative.

The tools intentionally resolve chat_id from settings, not from
tool args — the whole point is that PILK knows WHO to message
without each agent having to re-learn the operator's identity.
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
        "incident, ad-campaign report ready. Every send is COMMS-"
        "risk, so it queues for approval by default. Messages over "
        "4096 chars are truncated; use telegram_deliver for long "
        "content."
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
        },
        "required": ["text"],
    },
    risk=RiskClass.COMMS,
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
        "enforces scope. COMMS-risk — queues for approval."
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
    risk=RiskClass.COMMS,
    handler=_deliver,
)


TELEGRAM_TOOLS: list[Tool] = [telegram_notify_tool, telegram_deliver_tool]
