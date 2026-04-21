"""Notion — read + write via the official REST API.

Two tools exposed to the model:

    notion_read   (NET_READ)   fetch a page's plain-text content
    notion_write  (NET_WRITE)  create a new page, or append content to one

### Auth

Internal integration token (``notion_api_key``) resolved via the
integration-secrets store with env-var fallback (``NOTION_API_KEY``).
The operator creates an integration at notion.com/my-integrations,
pastes the secret into Settings → API Keys, then shares specific
pages / databases with the integration. Nothing is accessible by
default — Notion's permission model is opt-in per-page.

### Content shaping

Notion content is a tree of blocks. On read we flatten the
top-level children of a page into plain text (paragraph headings,
bullets, quotes, todos, code fences). Nested blocks and rich-text
styling are dropped — a planner turn wants the gist, not the raw
block tree. Unsupported blocks (embeds, images, databases) render
as a placeholder so the model sees *something* is there without us
having to parse every block type.

On write we accept a plain-text ``content`` string and split it
into paragraph blocks on blank-line boundaries. No markdown parsing
in V1 — an operator who needs headings / lists can call
``notion_write`` repeatedly or use the Notion UI. Keeping the
contract simple means the tool works predictably.

### Rate limits

Notion caps to ~3 req/s per integration. Not enforced client-side
for V1 — single-operator traffic is nowhere near that. A 429
surfaces via the standard error path; if real usage shows 429s we
can add backoff later without changing the tool contract.
"""

from __future__ import annotations

from typing import Any

import httpx

from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.notion")

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2022-06-28"
DEFAULT_TIMEOUT_S = 30.0
# Hard cap on the plain-text body we'll echo back from notion_read.
# Past this the planner is better off using the browser than cramming
# a whole wiki page into a tool output.
MAX_READ_CHARS = 8000
# Hard cap on the content a single notion_write call can ship. Notion
# itself accepts much more but 6KB is plenty for a tool call; longer
# content should be chunked across multiple appends anyway.
MAX_WRITE_CHARS = 6000
# Notion caps appends at 100 blocks per children.append call. We split
# the paragraph array into chunks of this size before sending.
MAX_BLOCKS_PER_APPEND = 100


class NotionError(Exception):
    """Raised on Notion API error (non-2xx). Wraps the HTTP status +
    the ``message`` field from Notion's JSON body so handlers can
    surface clean copy."""

    def __init__(self, status: int, message: str, raw: Any = None) -> None:
        super().__init__(f"Notion {status}: {message}")
        self.status = status
        self.message = message
        self.raw = raw


# ── async client ─────────────────────────────────────────────────


class NotionClient:
    """Thin httpx wrapper — no caching, no retry. Each method is
    one HTTP round-trip.

    Constructed per-tool-call (cheap) so a runtime secret rotation
    lands without a daemon restart. Tool handlers resolve the live
    token via ``resolve_secret`` and instantiate a fresh client
    each invocation.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str = NOTION_API_BASE,
        api_version: str = NOTION_API_VERSION,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._base = api_base.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": api_version,
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    async def get_page(self, page_id: str) -> dict[str, Any]:
        """Fetch page metadata (properties, parent, created_time)."""
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(
                f"{self._base}/pages/{page_id}",
                headers=self._headers,
            )
        return _decode(r, "get_page")

    async def get_page_children(
        self, page_id: str, *, page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch the top-level blocks that make up a page's body.

        Single-page fetch — we don't follow ``next_cursor`` in V1.
        100 blocks is plenty for a planner turn; operators with
        thousand-block pages should narrow via ``page_id`` into a
        specific section.
        """
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(
                f"{self._base}/blocks/{page_id}/children",
                headers=self._headers,
                params={"page_size": int(page_size)},
            )
        body = _decode(r, "get_page_children")
        results = body.get("results")
        return list(results) if isinstance(results, list) else []

    async def create_page(
        self,
        *,
        parent_page_id: str,
        title: str,
        blocks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "parent": {"page_id": parent_page_id},
            "properties": {
                "title": {
                    "title": [{"text": {"content": title}}],
                },
            },
            "children": blocks,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(
                f"{self._base}/pages",
                headers=self._headers,
                json=payload,
            )
        return _decode(r, "create_page")

    async def append_blocks(
        self, page_id: str, blocks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for chunk in _chunks(blocks, MAX_BLOCKS_PER_APPEND):
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                r = await c.patch(
                    f"{self._base}/blocks/{page_id}/children",
                    headers=self._headers,
                    json={"children": chunk},
                )
            last = _decode(r, "append_blocks")
        return last


def _decode(resp: httpx.Response, method: str) -> dict[str, Any]:
    """Uniform error-decode step. Notion returns
    ``{"object":"error","status":N,"code":"...","message":"..."}``
    on failure; hoist that into :class:`NotionError` so every
    tool handler sees one exception shape."""
    try:
        body = resp.json()
    except ValueError:
        raise NotionError(
            status=resp.status_code,
            message=f"{method}: non-JSON response ({resp.text[:160]!r})",
        ) from None
    if resp.is_success:
        return body if isinstance(body, dict) else {}
    message = (
        body.get("message")
        if isinstance(body, dict) else None
    ) or f"HTTP {resp.status_code}"
    raise NotionError(
        status=resp.status_code, message=message, raw=body,
    )


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ── block rendering (read) ───────────────────────────────────────


def _render_rich_text(rt: list[dict[str, Any]] | None) -> str:
    if not rt:
        return ""
    return "".join(
        (r.get("plain_text") or "") for r in rt if isinstance(r, dict)
    )


def blocks_to_plaintext(blocks: list[dict[str, Any]]) -> str:
    """Flatten a list of top-level Notion blocks into plain text.

    Supported: paragraph, heading_1/2/3, bulleted_list_item,
    numbered_list_item, to_do, quote, code, divider, callout.
    Everything else renders as ``[unsupported: <type>]`` so the
    planner sees that something exists without us having to parse
    every corner of the block schema.
    """
    lines: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "paragraph":
            lines.append(_render_rich_text(
                (b.get("paragraph") or {}).get("rich_text")
            ))
        elif t == "heading_1":
            text = _render_rich_text((b.get("heading_1") or {}).get("rich_text"))
            lines.append(f"# {text}")
        elif t == "heading_2":
            text = _render_rich_text((b.get("heading_2") or {}).get("rich_text"))
            lines.append(f"## {text}")
        elif t == "heading_3":
            text = _render_rich_text((b.get("heading_3") or {}).get("rich_text"))
            lines.append(f"### {text}")
        elif t == "bulleted_list_item":
            text = _render_rich_text(
                (b.get("bulleted_list_item") or {}).get("rich_text")
            )
            lines.append(f"- {text}")
        elif t == "numbered_list_item":
            text = _render_rich_text(
                (b.get("numbered_list_item") or {}).get("rich_text")
            )
            lines.append(f"1. {text}")
        elif t == "to_do":
            inner = b.get("to_do") or {}
            text = _render_rich_text(inner.get("rich_text"))
            checked = inner.get("checked")
            marker = "[x]" if checked else "[ ]"
            lines.append(f"{marker} {text}")
        elif t == "quote":
            text = _render_rich_text((b.get("quote") or {}).get("rich_text"))
            lines.append(f"> {text}")
        elif t == "code":
            inner = b.get("code") or {}
            text = _render_rich_text(inner.get("rich_text"))
            lang = inner.get("language") or ""
            lines.append(f"```{lang}\n{text}\n```")
        elif t == "callout":
            text = _render_rich_text(
                (b.get("callout") or {}).get("rich_text")
            )
            lines.append(f"📌 {text}")
        elif t == "divider":
            lines.append("---")
        else:
            lines.append(f"[unsupported block: {t}]")
    return "\n".join(lines)


# ── block building (write) ───────────────────────────────────────


def content_to_paragraphs(content: str) -> list[dict[str, Any]]:
    """Split a plain-text body into Notion paragraph blocks.

    Blank-line boundaries delimit paragraphs. Empty paragraphs are
    preserved as empty blocks so visual spacing round-trips; Notion
    renders those as blank lines.
    """
    paragraphs = content.split("\n\n")
    out: list[dict[str, Any]] = []
    for p in paragraphs:
        text = p.strip("\n")
        out.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": (
                        [{"type": "text", "text": {"content": text}}]
                        if text else []
                    ),
                },
            }
        )
    return out


# ── tool factories ───────────────────────────────────────────────


def _client_or_error() -> NotionClient | ToolOutcome:
    from core.config import get_settings
    from core.secrets import resolve_secret

    settings = get_settings()
    api_key = resolve_secret("notion_api_key", settings.notion_api_key)
    if not api_key:
        return ToolOutcome(
            content=(
                "Notion not configured. Add notion_api_key in "
                "Settings → API Keys (create an integration at "
                "notion.com/my-integrations, then share each page "
                "with the integration)."
            ),
            is_error=True,
        )
    return NotionClient(api_key=api_key)


def make_notion_tools() -> list[Tool]:
    async def _read(args: dict, _ctx: ToolContext) -> ToolOutcome:
        page_id = str(args.get("page_id") or "").strip()
        if not page_id:
            return ToolOutcome(
                content="notion_read requires a 'page_id'.",
                is_error=True,
            )
        client_or_err = _client_or_error()
        if isinstance(client_or_err, ToolOutcome):
            return client_or_err
        try:
            blocks = await client_or_err.get_page_children(page_id)
        except NotionError as e:
            return _surface(e, "notion_read")
        except Exception as e:
            log.exception("notion_read_unexpected")
            return ToolOutcome(
                content=f"notion_read failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        text = blocks_to_plaintext(blocks)
        truncated = len(text) > MAX_READ_CHARS
        shown = text[:MAX_READ_CHARS]
        suffix = (
            f"\n\n[truncated — {len(text)} chars, shown {MAX_READ_CHARS}]"
            if truncated else ""
        )
        return ToolOutcome(
            content=(
                f"{shown}{suffix}"
                if text else "[page has no supported content blocks]"
            ),
            data={
                "page_id": page_id,
                "chars": len(text),
                "blocks": len(blocks),
            },
        )

    async def _write(args: dict, _ctx: ToolContext) -> ToolOutcome:
        content = str(args.get("content") or "")
        if not content.strip():
            return ToolOutcome(
                content="notion_write requires non-empty 'content'.",
                is_error=True,
            )
        if len(content) > MAX_WRITE_CHARS:
            return ToolOutcome(
                content=(
                    f"notion_write content too long ({len(content)} "
                    f"> {MAX_WRITE_CHARS}). Split into multiple "
                    "appends."
                ),
                is_error=True,
            )
        page_id = str(args.get("page_id") or "").strip()
        parent_page_id = str(args.get("parent_page_id") or "").strip()
        title = str(args.get("title") or "").strip()
        if not page_id and not parent_page_id:
            return ToolOutcome(
                content=(
                    "notion_write requires 'page_id' (append to an "
                    "existing page) OR 'parent_page_id' + 'title' "
                    "(create a new page under a parent)."
                ),
                is_error=True,
            )
        if page_id and parent_page_id:
            return ToolOutcome(
                content=(
                    "notion_write takes 'page_id' OR 'parent_page_id', "
                    "not both. Append to an existing page or create a "
                    "new one — pick one."
                ),
                is_error=True,
            )
        if parent_page_id and not title:
            return ToolOutcome(
                content=(
                    "notion_write requires 'title' when creating a "
                    "new page under 'parent_page_id'."
                ),
                is_error=True,
            )
        client_or_err = _client_or_error()
        if isinstance(client_or_err, ToolOutcome):
            return client_or_err
        blocks = content_to_paragraphs(content)
        try:
            if page_id:
                await client_or_err.append_blocks(page_id, blocks)
                return ToolOutcome(
                    content=(
                        f"Appended {len(blocks)} paragraph block(s) "
                        f"to Notion page {page_id}."
                    ),
                    data={
                        "action": "append",
                        "page_id": page_id,
                        "blocks": len(blocks),
                    },
                )
            created = await client_or_err.create_page(
                parent_page_id=parent_page_id,
                title=title,
                blocks=blocks,
            )
            new_id = created.get("id") or ""
            url = created.get("url") or ""
            return ToolOutcome(
                content=(
                    f"Created Notion page \"{title}\" "
                    f"({new_id[:12]}…). {url}"
                ),
                data={
                    "action": "create",
                    "page_id": new_id,
                    "parent_page_id": parent_page_id,
                    "title": title,
                    "url": url,
                    "blocks": len(blocks),
                },
            )
        except NotionError as e:
            return _surface(e, "notion_write")
        except Exception as e:
            log.exception("notion_write_unexpected")
            return ToolOutcome(
                content=f"notion_write failed: {type(e).__name__}: {e}",
                is_error=True,
            )

    read_tool = Tool(
        name="notion_read",
        description=(
            "Fetch a Notion page by id and return its plain-text "
            "content (top-level blocks flattened). Supports "
            "paragraphs, headings, bullets, todos, quotes, and code "
            "fences. Useful for grabbing a playbook, meeting notes, "
            "or brief you wrote in Notion. The page must be shared "
            "with PILK's integration in Notion first (Settings → "
            "API Keys → Notion has the setup link). Caps output at "
            f"{MAX_READ_CHARS} chars."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": (
                        "Notion page id — the long hex string at the "
                        "end of a Notion URL, e.g. the part after "
                        "the title in notion.so/WorkspaceName/"
                        "MyPage-<page_id>."
                    ),
                },
            },
            "required": ["page_id"],
        },
        risk=RiskClass.NET_READ,
        handler=_read,
    )
    write_tool = Tool(
        name="notion_write",
        description=(
            "Append text to an existing Notion page OR create a new "
            "page under a parent. Pass 'page_id' to append; pass "
            "'parent_page_id' + 'title' to create a new child page. "
            "Content is split on blank-line boundaries into paragraph "
            "blocks (no markdown parsing in V1). NET_WRITE risk — "
            "changes persist to the operator's Notion workspace and "
            "can be seen by anyone with page access."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": (
                        "Target page id for APPEND. Mutually "
                        "exclusive with parent_page_id."
                    ),
                },
                "parent_page_id": {
                    "type": "string",
                    "description": (
                        "Parent page id for CREATE. Mutually "
                        "exclusive with page_id."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Title for the new page. Required when "
                        "creating (parent_page_id set)."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        f"Plain-text body. Cap "
                        f"{MAX_WRITE_CHARS} chars."
                    ),
                },
            },
            "required": ["content"],
        },
        risk=RiskClass.NET_WRITE,
        handler=_write,
    )
    return [read_tool, write_tool]


def _surface(e: NotionError, tool_name: str) -> ToolOutcome:
    # Rewrite the most common failures into actionable copy. The
    # raw message is still in data.raw so debugging doesn't lose
    # context.
    hint = e.message
    if e.status == 404:
        hint = (
            f"{e.message} — double-check the page_id, and make sure "
            "PILK's integration is shared with that page "
            "(page ⋯ menu → Add connections)."
        )
    elif e.status == 401:
        hint = (
            f"{e.message} — notion_api_key is invalid or missing. "
            "Reissue at notion.com/my-integrations and paste into "
            "Settings → API Keys."
        )
    elif e.status == 429:
        hint = (
            f"{e.message} — Notion rate limit hit (~3 req/s per "
            "integration). Back off and retry."
        )
    return ToolOutcome(
        content=f"{tool_name} failed: Notion {e.status}: {hint}",
        is_error=True,
        data={"status": e.status, "raw": e.raw},
    )


__all__ = [
    "MAX_READ_CHARS",
    "MAX_WRITE_CHARS",
    "NOTION_API_BASE",
    "NOTION_API_VERSION",
    "NotionClient",
    "NotionError",
    "blocks_to_plaintext",
    "content_to_paragraphs",
    "make_notion_tools",
]
