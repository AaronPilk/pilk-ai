"""X (Twitter) post tool — user role, never-trust.

One tool: x_post_as_me(text) — posts a tweet as the signed-in user.
COMMS risk; never-trust-whitelistable. `text` is capped at 280 chars
(standard tweet). Threading and media upload are deferred.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request

from core.identity import AccountsStore
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import AccountBinding, Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.x.post")

POST_URL = "https://api.twitter.com/2/tweets"
MAX_CHARS = 280


def make_x_tools(accounts: AccountsStore) -> list[Tool]:
    binding = AccountBinding(provider="x", role="user")

    not_linked = ToolOutcome(
        content=(
            "X isn't connected yet. Open Settings → Connected accounts "
            "and link your X account."
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
        text = str(args.get("text") or "").strip()
        if not text:
            return ToolOutcome(
                content="x_post_as_me requires non-empty 'text'.",
                is_error=True,
            )
        if len(text) > MAX_CHARS:
            return ToolOutcome(
                content=(
                    f"x_post_as_me: text is {len(text)} chars, max is "
                    f"{MAX_CHARS}. Trim it or split into a thread "
                    "(threading not supported yet)."
                ),
                is_error=True,
            )
        try:
            tweet = await asyncio.to_thread(_do_post, token, text)
        except Exception as e:
            log.exception("x_post_failed")
            return ToolOutcome(
                content=f"x_post_as_me failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=f"Posted on X (id {tweet['id']}).",
            data=tweet,
        )

    tool = Tool(
        name="x_post_as_me",
        description=(
            "Post a tweet as the signed-in X user. COMMS risk — every "
            "post routes through the approval queue so you review the "
            "text before it ships. 280 chars max; threading and media "
            "upload are not supported yet."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": f"Tweet body, up to {MAX_CHARS} chars.",
                },
            },
            "required": ["text"],
        },
        risk=RiskClass.COMMS,
        handler=_post,
        account_binding=binding,
    )
    return [tool]


def _do_post(token: str, text: str) -> dict:
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        POST_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", "replace")
    info = json.loads(raw)
    data = info.get("data") or {}
    tweet_id = data.get("id")
    if not tweet_id:
        raise RuntimeError(f"unexpected X response: {raw[:300]}")
    return {"id": tweet_id, "text": data.get("text", text)}
