"""LinkedIn post tool — user role, never-trust.

One tool: linkedin_post_as_me(text) — publishes a text post on the
signed-in user's LinkedIn profile. COMMS risk; the comms sub-policy
pins it to never-trust alongside gmail_send_as_me and
slack_post_as_me. Every post goes through fresh approval.

Posts use the /rest/posts endpoint (newer) rather than legacy
ugcPosts because it's the documented path for OIDC apps and handles
visibility + commentary in one call.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request

from core.identity import AccountsStore
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import AccountBinding, Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.linkedin.post")

POSTS_URL = "https://api.linkedin.com/rest/posts"
REST_VERSION = "202406"


def make_linkedin_tools(accounts: AccountsStore) -> list[Tool]:
    binding = AccountBinding(provider="linkedin", role="user")

    not_linked = ToolOutcome(
        content=(
            "LinkedIn isn't connected yet. Open Settings → Connected "
            "accounts and link LinkedIn."
        ),
        is_error=True,
    )

    def _load():
        account = accounts.resolve_binding(binding)
        if account is None:
            return None, None
        tokens = accounts.load_tokens(account.account_id)
        if tokens is None or not tokens.access_token:
            return None, account
        sub = (tokens.extra or {}).get("linkedin_sub") if tokens.extra else None
        return tokens.access_token, sub

    async def _post(args: dict, ctx: ToolContext) -> ToolOutcome:
        token, sub = _load()
        if token is None:
            return not_linked
        text = str(args.get("text") or "").strip()
        if not text:
            return ToolOutcome(
                content="linkedin_post_as_me requires non-empty 'text'.",
                is_error=True,
            )
        visibility = str(args.get("visibility") or "PUBLIC").upper()
        if visibility not in ("PUBLIC", "CONNECTIONS"):
            return ToolOutcome(
                content="visibility must be PUBLIC or CONNECTIONS.",
                is_error=True,
            )
        try:
            result = await asyncio.to_thread(
                _do_post, token, sub, text, visibility
            )
        except Exception as e:
            log.exception("linkedin_post_failed")
            return ToolOutcome(
                content=f"linkedin_post_as_me failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=f"Posted to LinkedIn (id {result['id']}).",
            data=result,
        )

    tool = Tool(
        name="linkedin_post_as_me",
        description=(
            "Publish a text post on your LinkedIn profile. COMMS risk — "
            "every post routes through the approval queue so you review "
            "the body and visibility before it ships. Visibility defaults "
            "to PUBLIC; pass 'CONNECTIONS' to limit to your network."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Post body."},
                "visibility": {
                    "type": "string",
                    "enum": ["PUBLIC", "CONNECTIONS"],
                    "description": "Who sees it. Defaults to PUBLIC.",
                },
            },
            "required": ["text"],
        },
        risk=RiskClass.COMMS,
        handler=_post,
        account_binding=binding,
    )
    return [tool]


def _do_post(token: str, sub: str | None, text: str, visibility: str) -> dict:
    if not sub:
        raise RuntimeError(
            "missing LinkedIn user id — re-link the account so profile "
            "fetch can capture your sub"
        )
    body = {
        "author": f"urn:li:person:{sub}",
        "commentary": text,
        "visibility": visibility,
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    req = urllib.request.Request(
        POSTS_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "LinkedIn-Version": REST_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        post_id = resp.headers.get("x-restli-id") or ""
        raw = resp.read().decode("utf-8", "replace")
    return {"id": post_id, "response": raw[:500]}
