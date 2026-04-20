"""Unit + tool tests for the Telegram integration.

httpx is stubbed via MockTransport so nothing hits the real bot API.
Coverage:

- Client: sendMessage URL + payload shape, chat_id resolution,
  truncation at 4096 chars, send_document multipart + file-missing
  handling, ok=false → TelegramError, non-2xx → TelegramError.
- Tools: registry shape, COMMS risk on every tool, "not configured"
  path surfaces a clear hint, arg validation (empty text / missing
  path / path escape), happy-path ToolOutcome shape.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from core.config import get_settings
from core.integrations.telegram import (
    TELEGRAM_MESSAGE_MAX_CHARS,
    TelegramClient,
    TelegramConfig,
    TelegramError,
)
from core.policy.risk import RiskClass
from core.tools.builtin.telegram import (
    TELEGRAM_TOOLS,
    telegram_deliver_tool,
    telegram_notify_tool,
)
from core.tools.registry import ToolContext


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


def _client() -> TelegramClient:
    return TelegramClient(
        TelegramConfig(bot_token="tok-abc", chat_id="999"),
    )


def _ok_result(result: dict) -> dict:
    return {"ok": True, "result": result}


# ── Client: sendMessage ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_url_and_payload() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _json
        seen["url"] = str(req.url)
        seen["body"] = _json.loads(req.content.decode())
        return httpx.Response(
            200, json=_ok_result({"message_id": 42, "chat": {"id": 999}}),
        )

    _install_transport(handler)
    result = await _client().send_message("hello")
    assert result["message_id"] == 42
    assert "/bot tok-abc".replace(" ", "") in seen["url"].replace(" ", "")
    assert seen["url"].endswith("/sendMessage")
    assert seen["body"]["text"] == "hello"
    assert seen["body"]["chat_id"] == "999"


@pytest.mark.asyncio
async def test_send_message_truncates_long_text() -> None:
    """Telegram caps at 4096 chars; we truncate cleanly instead of
    letting the API 400. The truncation is conservative — we leave
    room for the trailing hint so the operator sees there was more."""
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _json
        seen["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json=_ok_result({"message_id": 1}))

    _install_transport(handler)
    long_text = "x" * (TELEGRAM_MESSAGE_MAX_CHARS + 500)
    await _client().send_message(long_text)
    assert len(seen["body"]["text"]) <= TELEGRAM_MESSAGE_MAX_CHARS
    assert "truncated" in seen["body"]["text"].lower()


@pytest.mark.asyncio
async def test_send_message_chat_id_override() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _json
        seen["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json=_ok_result({"message_id": 1}))

    _install_transport(handler)
    await _client().send_message("hi", chat_id="111")
    assert seen["body"]["chat_id"] == "111"


@pytest.mark.asyncio
async def test_send_message_honours_parse_mode() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _json
        seen["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json=_ok_result({"message_id": 1}))

    _install_transport(handler)
    await _client().send_message("**bold**", parse_mode="MarkdownV2")
    assert seen["body"]["parse_mode"] == "MarkdownV2"


@pytest.mark.asyncio
async def test_send_message_surfaces_api_error_when_ok_false() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "description": "chat not found"},
        )

    _install_transport(handler)
    with pytest.raises(TelegramError) as exc:
        await _client().send_message("hi")
    assert "chat not found" in exc.value.message


@pytest.mark.asyncio
async def test_send_message_surfaces_non_2xx() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"ok": False, "description": "Unauthorized"},
        )

    _install_transport(handler)
    with pytest.raises(TelegramError) as exc:
        await _client().send_message("hi")
    assert exc.value.status == 401
    assert "Unauthorized" in exc.value.message


# ── Client: sendDocument ────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_document_rejects_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.pdf"
    with pytest.raises(TelegramError) as exc:
        await _client().send_document(missing)
    assert "not found" in exc.value.message


@pytest.mark.asyncio
async def test_send_document_multipart_shape(tmp_path: Path) -> None:
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.7\n%stub\n")

    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["content_type"] = req.headers.get("content-type", "")
        seen["body_bytes_len"] = len(req.content)
        return httpx.Response(200, json=_ok_result({"message_id": 7}))

    _install_transport(handler)
    result = await _client().send_document(f, caption="Here you go")
    assert result["message_id"] == 7
    assert seen["url"].endswith("/sendDocument")
    assert "multipart/form-data" in seen["content_type"]


@pytest.mark.asyncio
async def test_send_document_truncates_long_caption(tmp_path: Path) -> None:
    f = tmp_path / "x.bin"
    f.write_bytes(b"data")
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        # Form fields land in the body; multipart parser is heavy —
        # just assert the body contains a truncated caption by
        # finding the cap indicator.
        seen["body_snippet"] = req.content[:4096]
        return httpx.Response(200, json=_ok_result({"message_id": 1}))

    _install_transport(handler)
    await _client().send_document(f, caption="y" * 5000)
    # The caption should have been chopped to 1024 chars; we assert
    # on length by searching the multipart body for 'y' runs.
    longest_y_run = max(
        len(s) for s in seen["body_snippet"].split(b"\r\n") if s.startswith(b"y")
    )
    assert longest_y_run <= 1024


# ── Tools: registry shape + risk ────────────────────────────────


def test_tool_count_is_two() -> None:
    assert len(TELEGRAM_TOOLS) == 2


def test_tool_names_unique_and_prefixed() -> None:
    names = [t.name for t in TELEGRAM_TOOLS]
    assert len(names) == len(set(names))
    for n in names:
        assert n.startswith("telegram_")


def test_every_tool_is_comms_risk() -> None:
    """Push channel = human notification = COMMS. Every tool in the
    telegram bundle must trip the same approval gate as email."""
    for t in TELEGRAM_TOOLS:
        assert t.risk == RiskClass.COMMS, t.name


# ── Tools: not-configured ───────────────────────────────────────


def _clear_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    for k in (
        "TELEGRAM_BOT_TOKEN", "PILK_TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID", "PILK_TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(k, raising=False)


def _set_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(sandbox_root=tmp_path)


@pytest.mark.asyncio
async def test_notify_without_keys_returns_clean_hint(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext,
) -> None:
    _clear_creds(monkeypatch)
    out = await telegram_notify_tool.handler({"text": "hello"}, ctx)
    assert out.is_error
    assert "Telegram not configured" in out.content
    # The hint should tell the operator EXACTLY which keys to paste.
    assert "telegram_bot_token" in out.content
    assert "telegram_chat_id" in out.content
    # And how to get them, so an operator can unblock themselves.
    assert "@BotFather" in out.content


@pytest.mark.asyncio
async def test_deliver_without_keys_returns_clean_hint(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext, tmp_path: Path,
) -> None:
    _clear_creds(monkeypatch)
    f = tmp_path / "x.pdf"
    f.write_bytes(b"data")
    out = await telegram_deliver_tool.handler({"path": "x.pdf"}, ctx)
    assert out.is_error
    assert "Telegram not configured" in out.content


# ── Tools: arg validation ───────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_requires_text(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    out = await telegram_notify_tool.handler({}, ctx)
    assert out.is_error
    assert "text" in out.content


@pytest.mark.asyncio
async def test_deliver_requires_path(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    out = await telegram_deliver_tool.handler({}, ctx)
    assert out.is_error
    assert "path" in out.content


@pytest.mark.asyncio
async def test_deliver_rejects_workspace_escape(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    out = await telegram_deliver_tool.handler(
        {"path": "../../etc/passwd"}, ctx,
    )
    assert out.is_error
    assert "escapes workspace" in out.content


@pytest.mark.asyncio
async def test_deliver_rejects_missing_file(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    out = await telegram_deliver_tool.handler(
        {"path": "report.pdf"}, ctx,
    )
    assert out.is_error
    assert "not found" in out.content


# ── Tools: happy path ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_happy_path(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_result({"message_id": 101, "chat": {"id": 999}}),
        )

    _install_transport(handler)
    out = await telegram_notify_tool.handler(
        {"text": "Meta Ads campaign created — awaiting activation."},
        ctx,
    )
    assert not out.is_error
    assert out.data["message_id"] == 101


@pytest.mark.asyncio
async def test_deliver_happy_path(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext, tmp_path: Path,
) -> None:
    _set_creds(monkeypatch)
    f = tmp_path / "audit.pdf"
    f.write_bytes(b"%PDF-1.7\n%stub\n")

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_result({"message_id": 202}),
        )

    _install_transport(handler)
    out = await telegram_deliver_tool.handler(
        {"path": "audit.pdf", "caption": "Weekly ads audit"}, ctx,
    )
    assert not out.is_error
    assert out.data["message_id"] == 202
    assert out.data["path"] == "audit.pdf"
