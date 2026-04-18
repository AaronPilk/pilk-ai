"""Meta post tools — Facebook Page + Instagram Business, user-role.

Two tools, both COMMS / never-trust:

- facebook_post_to_page(text, page_id?)
  Posts to a Facebook Page. The Meta OAuth profile-fetcher stored the
  list of Pages you manage on the connected account; if you omit
  page_id we use the first managed Page.

- instagram_post_to_business(caption, image_url, ig_business_id?)
  Instagram requires a media URL — there is no text-only post. The
  image must be publicly reachable over HTTPS (Meta fetches it). The
  publish is a two-step dance (create container, then publish); this
  tool runs both synchronously.

Personal Facebook walls and personal Instagram accounts are not
supported by Meta's API — the tools refuse with a clear message if
no managed Page / no linked IG Business is found on the account.
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

log = get_logger("pilkd.meta.post")

GRAPH = "https://graph.facebook.com/v20.0"


def make_meta_tools(accounts: AccountsStore) -> list[Tool]:
    binding = AccountBinding(provider="meta", role="user")

    not_linked = ToolOutcome(
        content=(
            "Meta isn't connected yet. Open Settings → Connected accounts "
            "and link Meta (used for both Facebook Pages and Instagram "
            "Business)."
        ),
        is_error=True,
    )

    def _account():
        return accounts.resolve_binding(binding)

    def _pages() -> list[dict]:
        account = _account()
        if account is None:
            return []
        tokens = accounts.load_tokens(account.account_id)
        if tokens is None:
            return []
        return list((tokens.extra or {}).get("pages") or [])

    async def _post_page(args: dict, ctx: ToolContext) -> ToolOutcome:
        if _account() is None:
            return not_linked
        pages = _pages()
        if not pages:
            return ToolOutcome(
                content=(
                    "Meta is connected but you don't manage any Facebook "
                    "Pages on this account. Meta removed personal-profile "
                    "posting from the API in 2018 — this tool only works "
                    "with Pages."
                ),
                is_error=True,
            )
        text = str(args.get("text") or "").strip()
        if not text:
            return ToolOutcome(
                content="facebook_post_to_page requires non-empty 'text'.",
                is_error=True,
            )
        page_id = str(args.get("page_id") or "").strip() or None
        page = _pick(pages, page_id)
        if page is None:
            return ToolOutcome(
                content=(
                    f"No Page matches id {page_id!r}. Managed Pages: "
                    + ", ".join(
                        f"{p.get('name')} ({p.get('id')})" for p in pages
                    )
                ),
                is_error=True,
            )
        try:
            result = await asyncio.to_thread(
                _do_post_page,
                page["page_access_token"],
                page["id"],
                text,
            )
        except Exception as e:
            log.exception("facebook_post_failed")
            return ToolOutcome(
                content=f"facebook_post_to_page failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=(
                f"Posted to Facebook Page {page.get('name', page['id'])} "
                f"(post id {result['post_id']})."
            ),
            data=result,
        )

    async def _post_ig(args: dict, ctx: ToolContext) -> ToolOutcome:
        if _account() is None:
            return not_linked
        pages = _pages()
        ig_pages = [p for p in pages if p.get("ig_business_id")]
        if not ig_pages:
            return ToolOutcome(
                content=(
                    "No Instagram Business account is linked to any of "
                    "your managed Pages. Convert the IG account to "
                    "Business/Creator and link it to a Facebook Page, "
                    "then re-link Meta in Settings."
                ),
                is_error=True,
            )
        caption = str(args.get("caption") or "").strip()
        image_url = str(args.get("image_url") or "").strip()
        if not caption:
            return ToolOutcome(
                content="instagram_post_to_business requires a 'caption'.",
                is_error=True,
            )
        if not image_url.startswith("https://"):
            return ToolOutcome(
                content=(
                    "'image_url' must be a publicly reachable https:// URL. "
                    "Meta fetches the image from this URL during publish."
                ),
                is_error=True,
            )
        ig_id = str(args.get("ig_business_id") or "").strip() or None
        target = _pick_ig(ig_pages, ig_id)
        if target is None:
            return ToolOutcome(
                content=(
                    f"No Instagram Business account matches id {ig_id!r}. "
                    "Linked: "
                    + ", ".join(
                        f"{p.get('name')} ({p.get('ig_business_id')})"
                        for p in ig_pages
                    )
                ),
                is_error=True,
            )
        try:
            result = await asyncio.to_thread(
                _do_post_ig,
                target["page_access_token"],
                target["ig_business_id"],
                caption,
                image_url,
            )
        except Exception as e:
            log.exception("instagram_post_failed")
            return ToolOutcome(
                content=f"instagram_post_to_business failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=(
                f"Published on Instagram Business "
                f"{target.get('name')} (media id {result['media_id']})."
            ),
            data=result,
        )

    page_tool = Tool(
        name="facebook_post_to_page",
        description=(
            "Publish a text post on a Facebook Page you manage. COMMS "
            "risk — every post goes through the approval queue. Pass "
            "`page_id` to target a specific Page; otherwise the first "
            "managed Page is used."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Post body."},
                "page_id": {
                    "type": "string",
                    "description": "Optional Page id (as reported by Meta).",
                },
            },
            "required": ["text"],
        },
        risk=RiskClass.COMMS,
        handler=_post_page,
        account_binding=binding,
    )
    ig_tool = Tool(
        name="instagram_post_to_business",
        description=(
            "Publish an image post on your Instagram Business / Creator "
            "account. Requires a publicly reachable https image URL; "
            "text-only IG posts are not supported by the API. COMMS "
            "risk — every publish goes through the approval queue."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "caption": {"type": "string", "description": "Post caption."},
                "image_url": {
                    "type": "string",
                    "description": (
                        "Publicly reachable https image URL. Meta fetches "
                        "the image server-side during publish."
                    ),
                },
                "ig_business_id": {
                    "type": "string",
                    "description": (
                        "Optional Instagram Business account id (only "
                        "needed when multiple are linked)."
                    ),
                },
            },
            "required": ["caption", "image_url"],
        },
        risk=RiskClass.COMMS,
        handler=_post_ig,
        account_binding=binding,
    )
    return [page_tool, ig_tool]


def _pick(pages: list[dict], wanted_id: str | None) -> dict | None:
    if wanted_id is None:
        return next(
            (p for p in pages if p.get("id") and p.get("page_access_token")),
            None,
        )
    return next(
        (p for p in pages if str(p.get("id")) == wanted_id and p.get("page_access_token")),
        None,
    )


def _pick_ig(pages: list[dict], wanted_id: str | None) -> dict | None:
    if wanted_id is None:
        return next(
            (
                p
                for p in pages
                if p.get("ig_business_id") and p.get("page_access_token")
            ),
            None,
        )
    return next(
        (
            p
            for p in pages
            if str(p.get("ig_business_id")) == wanted_id
            and p.get("page_access_token")
        ),
        None,
    )


def _do_post_page(page_token: str, page_id: str, text: str) -> dict:
    body = urllib.parse.urlencode(
        {"message": text, "access_token": page_token}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{GRAPH}/{page_id}/feed",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        info = json.loads(resp.read().decode("utf-8", "replace"))
    post_id = info.get("id")
    if not post_id:
        raise RuntimeError(f"meta: unexpected response {info}")
    return {"post_id": post_id, "page_id": page_id}


def _do_post_ig(
    page_token: str, ig_business_id: str, caption: str, image_url: str
) -> dict:
    # 1) Create a media container.
    container_body = urllib.parse.urlencode(
        {
            "image_url": image_url,
            "caption": caption,
            "access_token": page_token,
        }
    ).encode("utf-8")
    container_req = urllib.request.Request(
        f"{GRAPH}/{ig_business_id}/media",
        data=container_body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(container_req, timeout=30) as resp:
        container = json.loads(resp.read().decode("utf-8", "replace"))
    container_id = container.get("id")
    if not container_id:
        raise RuntimeError(f"meta: container creation failed {container}")
    # 2) Publish the container.
    publish_body = urllib.parse.urlencode(
        {"creation_id": container_id, "access_token": page_token}
    ).encode("utf-8")
    publish_req = urllib.request.Request(
        f"{GRAPH}/{ig_business_id}/media_publish",
        data=publish_body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(publish_req, timeout=30) as resp:
        published = json.loads(resp.read().decode("utf-8", "replace"))
    media_id = published.get("id")
    if not media_id:
        raise RuntimeError(f"meta: publish failed {published}")
    return {
        "media_id": media_id,
        "container_id": container_id,
        "ig_business_id": ig_business_id,
    }
