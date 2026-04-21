"""Tests for the Notion integration.

Splits into three tiers:
- pure unit tests for ``blocks_to_plaintext`` + ``content_to_paragraphs``
- ``NotionClient`` round-trip with ``httpx.MockTransport``
- tool-handler happy + error paths

Every HTTP round-trip is mocked — no real API calls. The tool handlers
resolve the API key via ``resolve_secret``, so we set the env fallback
from the test.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from core.config import get_settings
from core.integrations.notion import (
    MAX_READ_CHARS,
    MAX_WRITE_CHARS,
    NotionClient,
    NotionError,
    blocks_to_plaintext,
    content_to_paragraphs,
    make_notion_tools,
)
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext

# ── httpx mock plumbing ──────────────────────────────────────────


def _install_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _restore_httpx_init():
    original = httpx.AsyncClient.__init__
    yield
    httpx.AsyncClient.__init__ = original  # type: ignore[method-assign]


@pytest.fixture
def notion_key(monkeypatch) -> str:
    monkeypatch.setenv("NOTION_API_KEY", "secret_test")
    get_settings.cache_clear()
    yield "secret_test"
    get_settings.cache_clear()


def _para(text: str) -> dict:
    return {
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{"plain_text": text}],
        },
    }


# ── blocks_to_plaintext ──────────────────────────────────────────


def test_paragraph_and_headings() -> None:
    blocks = [
        {
            "type": "heading_1",
            "heading_1": {"rich_text": [{"plain_text": "Title"}]},
        },
        _para("A plain paragraph."),
        {
            "type": "heading_2",
            "heading_2": {"rich_text": [{"plain_text": "Sub"}]},
        },
    ]
    out = blocks_to_plaintext(blocks)
    assert out == "# Title\nA plain paragraph.\n## Sub"


def test_bullets_and_todos() -> None:
    blocks = [
        {
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"plain_text": "b1"}]},
        },
        {
            "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": [{"plain_text": "n1"}]},
        },
        {
            "type": "to_do",
            "to_do": {
                "rich_text": [{"plain_text": "done"}],
                "checked": True,
            },
        },
        {
            "type": "to_do",
            "to_do": {
                "rich_text": [{"plain_text": "todo"}],
                "checked": False,
            },
        },
    ]
    out = blocks_to_plaintext(blocks)
    assert out == "- b1\n1. n1\n[x] done\n[ ] todo"


def test_code_block_preserves_language() -> None:
    blocks = [
        {
            "type": "code",
            "code": {
                "rich_text": [{"plain_text": "print('x')"}],
                "language": "python",
            },
        },
    ]
    assert blocks_to_plaintext(blocks) == "```python\nprint('x')\n```"


def test_unsupported_block_type_surfaces_placeholder() -> None:
    blocks = [{"type": "video", "video": {}}]
    assert blocks_to_plaintext(blocks) == "[unsupported block: video]"


def test_empty_rich_text_is_empty_line() -> None:
    blocks = [_para("")]
    assert blocks_to_plaintext(blocks) == ""


def test_divider_and_callout() -> None:
    blocks = [
        {"type": "divider", "divider": {}},
        {
            "type": "callout",
            "callout": {"rich_text": [{"plain_text": "note"}]},
        },
        {
            "type": "quote",
            "quote": {"rich_text": [{"plain_text": "said thing"}]},
        },
    ]
    out = blocks_to_plaintext(blocks)
    assert "---" in out
    assert "📌 note" in out
    assert "> said thing" in out


# ── content_to_paragraphs ────────────────────────────────────────


def test_single_paragraph() -> None:
    blocks = content_to_paragraphs("just one paragraph")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "paragraph"
    rt = blocks[0]["paragraph"]["rich_text"]
    assert rt[0]["text"]["content"] == "just one paragraph"


def test_blank_line_splits_paragraphs() -> None:
    blocks = content_to_paragraphs("first\n\nsecond\n\nthird")
    assert len(blocks) == 3
    texts = [
        b["paragraph"]["rich_text"][0]["text"]["content"]
        for b in blocks
    ]
    assert texts == ["first", "second", "third"]


def test_empty_paragraph_preserved_as_blank_block() -> None:
    """Empty paragraphs render as visual spacing in Notion; we
    preserve them so the round-trip stays faithful."""
    blocks = content_to_paragraphs("first\n\n\n\nsecond")
    # "first", "", "", "second" — blank-line boundaries create empties
    assert len(blocks) >= 3
    # The middle block(s) have empty rich_text arrays.
    assert any(
        b["paragraph"]["rich_text"] == [] for b in blocks
    )


# ── NotionClient ─────────────────────────────────────────────────


def _client() -> NotionClient:
    return NotionClient(api_key="secret_test")


@pytest.mark.asyncio
async def test_client_get_page_children_happy() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200,
            json={"results": [_para("hello")]},
        )

    _install_transport(handler)
    blocks = await _client().get_page_children("p-abc")
    assert len(blocks) == 1
    # Auth header + Notion-Version were attached.
    assert captured[0].headers["Authorization"] == "Bearer secret_test"
    assert "Notion-Version" in captured[0].headers
    assert captured[0].url.path.endswith("/blocks/p-abc/children")


@pytest.mark.asyncio
async def test_client_surfaces_api_error_as_notion_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "object": "error",
                "status": 404,
                "code": "object_not_found",
                "message": "Could not find page with ID …",
            },
        )

    _install_transport(handler)
    with pytest.raises(NotionError) as info:
        await _client().get_page("p-missing")
    assert info.value.status == 404
    assert "Could not find" in info.value.message


@pytest.mark.asyncio
async def test_client_create_page_posts_right_shape() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["url"] = str(req.url)
        import json as _json
        captured["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json={"id": "new-page-id", "url": "u"})

    _install_transport(handler)
    blocks = content_to_paragraphs("body")
    result = await _client().create_page(
        parent_page_id="par-1", title="Title", blocks=blocks,
    )
    assert result["id"] == "new-page-id"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/pages")
    assert captured["body"]["parent"] == {"page_id": "par-1"}
    assert (
        captured["body"]["properties"]["title"]["title"][0]["text"]["content"]
        == "Title"
    )


@pytest.mark.asyncio
async def test_client_append_blocks_chunks_over_cap() -> None:
    """More than 100 blocks → multiple PATCH calls."""
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _json
        body = _json.loads(req.content.decode())
        calls.append(len(body["children"]))
        return httpx.Response(200, json={"object": "list"})

    _install_transport(handler)
    # 150 blocks → two PATCHes (100 + 50).
    many = [_para(f"line {i}") for i in range(150)]
    await _client().append_blocks("p-1", many)
    assert calls == [100, 50]


# ── notion_read tool ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_missing_page_id() -> None:
    [read, _write] = make_notion_tools()
    out = await read.handler({}, ToolContext())
    assert out.is_error
    assert "page_id" in out.content.lower()


@pytest.mark.asyncio
async def test_read_not_configured() -> None:
    """When no API key is set, the tool surfaces the "Notion not
    configured" hint without hitting the network."""
    get_settings.cache_clear()
    [read, _write] = make_notion_tools()
    out = await read.handler({"page_id": "p-1"}, ToolContext())
    assert out.is_error
    assert "not configured" in out.content.lower()


@pytest.mark.asyncio
async def test_read_happy_path(notion_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "type": "heading_1",
                        "heading_1": {"rich_text": [{"plain_text": "H"}]},
                    },
                    _para("A body paragraph."),
                ],
            },
        )

    _install_transport(handler)
    [read, _write] = make_notion_tools()
    out = await read.handler({"page_id": "p-xyz"}, ToolContext())
    assert not out.is_error
    assert "# H" in out.content
    assert "A body paragraph." in out.content
    assert out.data["blocks"] == 2


@pytest.mark.asyncio
async def test_read_truncates_long_output(notion_key: str) -> None:
    """The plain-text render caps at MAX_READ_CHARS with a clear
    suffix so the planner knows it's seeing a prefix."""
    long_text = "x" * (MAX_READ_CHARS + 500)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [_para(long_text)]},
        )

    _install_transport(handler)
    [read, _write] = make_notion_tools()
    out = await read.handler({"page_id": "p"}, ToolContext())
    assert "truncated" in out.content.lower()
    # Content is capped roughly at the limit + suffix.
    assert len(out.content) < MAX_READ_CHARS + 200


@pytest.mark.asyncio
async def test_read_404_rewritten_to_sharing_hint(notion_key: str) -> None:
    """The most common Notion failure is "integration isn't shared
    with this page". The tool rewrites the 404 message to point the
    operator at that fix."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "object": "error",
                "status": 404,
                "code": "object_not_found",
                "message": "Could not find page with ID p-ghost.",
            },
        )

    _install_transport(handler)
    [read, _write] = make_notion_tools()
    out = await read.handler({"page_id": "p-ghost"}, ToolContext())
    assert out.is_error
    assert "add connections" in out.content.lower()


# ── notion_write tool ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_missing_content(notion_key: str) -> None:
    [_read, write] = make_notion_tools()
    out = await write.handler(
        {"page_id": "p"}, ToolContext(),
    )
    assert out.is_error
    assert "content" in out.content.lower()


@pytest.mark.asyncio
async def test_write_requires_page_id_or_parent(notion_key: str) -> None:
    [_read, write] = make_notion_tools()
    out = await write.handler(
        {"content": "hi"}, ToolContext(),
    )
    assert out.is_error
    assert "page_id" in out.content.lower() or "parent_page_id" in out.content.lower()


@pytest.mark.asyncio
async def test_write_rejects_both_page_and_parent(notion_key: str) -> None:
    [_read, write] = make_notion_tools()
    out = await write.handler(
        {
            "page_id": "p", "parent_page_id": "par",
            "title": "T", "content": "hi",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "not both" in out.content.lower()


@pytest.mark.asyncio
async def test_write_requires_title_on_create(notion_key: str) -> None:
    [_read, write] = make_notion_tools()
    out = await write.handler(
        {"parent_page_id": "par", "content": "hi"},
        ToolContext(),
    )
    assert out.is_error
    assert "title" in out.content.lower()


@pytest.mark.asyncio
async def test_write_caps_oversized_content(notion_key: str) -> None:
    [_read, write] = make_notion_tools()
    out = await write.handler(
        {"page_id": "p", "content": "x" * (MAX_WRITE_CHARS + 1)},
        ToolContext(),
    )
    assert out.is_error
    assert "too long" in out.content.lower()


@pytest.mark.asyncio
async def test_write_append_happy_path(notion_key: str) -> None:
    calls: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _json
        calls.append(
            {
                "method": req.method,
                "url": str(req.url),
                "body": _json.loads(req.content.decode()),
            }
        )
        return httpx.Response(200, json={"object": "list"})

    _install_transport(handler)
    [_read, write] = make_notion_tools()
    out = await write.handler(
        {"page_id": "p-1", "content": "alpha\n\nbeta"},
        ToolContext(),
    )
    assert not out.is_error
    assert out.data["action"] == "append"
    assert out.data["page_id"] == "p-1"
    # Two paragraph blocks.
    assert out.data["blocks"] == 2
    assert calls[0]["method"] == "PATCH"
    assert calls[0]["url"].endswith("/blocks/p-1/children")


@pytest.mark.asyncio
async def test_write_create_happy_path(notion_key: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST"
        return httpx.Response(
            200,
            json={
                "id": "new-abc",
                "url": "https://notion.so/new-abc",
            },
        )

    _install_transport(handler)
    [_read, write] = make_notion_tools()
    out = await write.handler(
        {
            "parent_page_id": "par-1",
            "title": "New Page",
            "content": "body",
        },
        ToolContext(),
    )
    assert not out.is_error
    assert out.data["action"] == "create"
    assert out.data["page_id"] == "new-abc"
    assert out.data["title"] == "New Page"
    assert "notion.so/new-abc" in out.content


# ── tool surface shape ───────────────────────────────────────────


def test_tool_metadata_and_risk() -> None:
    [read, write] = make_notion_tools()
    assert read.name == "notion_read"
    assert write.name == "notion_write"
    assert read.risk == RiskClass.NET_READ
    assert write.risk == RiskClass.NET_WRITE
