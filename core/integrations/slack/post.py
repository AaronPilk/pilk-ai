"""Slack posting tool — user role, never-trust.

One tool in this batch:

- slack_post_as_me(channel, text) — post a message to a channel or DM
  as the signed-in user. COMMS risk. The comms sub-policy pins this to
  never-trust alongside gmail_send_as_me: every message goes through
  fresh approval with the channel + text visible.

`channel` accepts either a Slack channel ID (C0123) or a name like
`#general`. Slack's `chat.postMessage` endpoint handles both.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request

from core.identity import AccountsStore
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import AccountBinding, Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.slack.post")

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


def make_slack_tools(accounts: AccountsStore) -> list[Tool]:
    binding = AccountBinding(provider="slack", role="user")

    not_linked = ToolOutcome(
        content=(
            "Slack isn't connected yet. Open Settings → Connected accounts "
            "and link a Slack workspace."
        ),
        is_error=True,
    )

    def _load_token() -> str | None:
        account = accounts.resolve_binding(binding)
        if account is None:
            return None
        tokens = accounts.load_tokens(account.account_id)
        if tokens is None or not tokens.access_token:
            return None
        return tokens.access_token

    async def _post(args: dict, ctx: ToolContext) -> ToolOutcome:
        token = _load_token()
        if token is None:
            return not_linked
        channel = str(args.get("channel") or "").strip()
        text = str(args.get("text") or "")
        if not channel:
            return ToolOutcome(
                content="slack_post_as_me requires a 'channel'.",
                is_error=True,
            )
        if not text.strip():
            return ToolOutcome(
                content="slack_post_as_me requires non-empty 'text'.",
                is_error=True,
            )
        try:
            response = await asyncio.to_thread(
                _do_post, token, channel, text
            )
        except Exception as e:
            log.exception("slack_post_failed")
            return ToolOutcome(
                content=f"slack_post_as_me failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        ts = response.get("ts", "")
        posted_channel = response.get("channel", channel)
        return ToolOutcome(
            content=f"Posted to {posted_channel} at ts {ts}.",
            data={"channel": posted_channel, "ts": ts},
        )

    post_tool = Tool(
        name="slack_post_as_me",
        description=(
            "Post a message to a Slack channel or DM as the signed-in user. "
            "`channel` accepts an ID (C0123) or name (#general / @alice). "
            "COMMS risk — every post routes through the approval queue so "
            "you can review the channel and body before it ships."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Channel ID, #channel-name, or @user.",
                },
                "text": {
                    "type": "string",
                    "description": "Message body. Plain text; Slack mrkdwn is supported.",
                },
            },
            "required": ["channel", "text"],
        },
        risk=RiskClass.COMMS,
        handler=_post,
        account_binding=binding,
    )
    return [post_tool]


# ── synchronous Slack helpers ─────────────────────────────────────────


def _do_post(token: str, channel: str, text: str) -> dict:
    body = urllib.parse.urlencode({"channel": channel, "text": text}).encode(
        "utf-8"
    )
    req = urllib.request.Request(
        POST_MESSAGE_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", "replace")
    info = json.loads(raw)
    if not info.get("ok"):
        raise RuntimeError(f"slack: {info.get('error', 'unknown error')}")
    return info
