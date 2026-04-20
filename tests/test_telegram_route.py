"""HTTP tests for the /telegram/* connect-flow routes.

Covers bot-info (configured/valid/invalid paths), detect-chat (empty
updates + happy path + stale-first-takes-most-recent), send-test
(missing creds → 400, success path, Telegram API error surfaced).

Network is stubbed via httpx.MockTransport; the route handlers run
in-process against a FakeRequest with app.state.brain irrelevant
(these routes don't touch the vault).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi import HTTPException

from core.api.routes import telegram as telegram_route
from core.config import get_settings


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


class _FakeRequest:
    """Minimal stand-in — the telegram routes don't touch app.state."""


def _set_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")


def _set_token_only(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-abc")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("PILK_TELEGRAM_CHAT_ID", raising=False)


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    for k in (
        "TELEGRAM_BOT_TOKEN", "PILK_TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID", "PILK_TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(k, raising=False)


# ── bot-info ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bot_info_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    r = await telegram_route.get_bot_info(_FakeRequest())
    assert r["configured"] is False
    assert "telegram_bot_token" in r["error"]


@pytest.mark.asyncio
async def test_bot_info_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_token_only(monkeypatch)

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/getMe")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "id": 12345,
                    "username": "pilk_bot",
                    "first_name": "PILK",
                    "can_join_groups": True,
                },
            },
        )

    _install_transport(handler)
    r = await telegram_route.get_bot_info(_FakeRequest())
    assert r["configured"] is True
    assert r["valid"] is True
    assert r["username"] == "pilk_bot"
    # The UI deep-links via this URL, so it must be the canonical one.
    assert r["t_me_url"] == "https://t.me/pilk_bot"


@pytest.mark.asyncio
async def test_bot_info_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_token_only(monkeypatch)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"ok": False, "description": "Unauthorized"},
        )

    _install_transport(handler)
    r = await telegram_route.get_bot_info(_FakeRequest())
    assert r["configured"] is True
    assert r["valid"] is False
    assert "Unauthorized" in r["error"]


@pytest.mark.asyncio
async def test_bot_info_network_error_hits_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_token_only(monkeypatch)

    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns")

    _install_transport(handler)
    with pytest.raises(HTTPException) as exc:
        await telegram_route.get_bot_info(_FakeRequest())
    assert exc.value.status_code == 502


# ── detect-chat ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detect_chat_picks_most_recent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_token_only(monkeypatch)

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/getUpdates")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    # Stale update first — we should NOT pick this one.
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": 111, "first_name": "Stale"},
                        },
                    },
                    # Newer update — the "/start" the operator just
                    # sent. Must take precedence.
                    {
                        "update_id": 2,
                        "message": {
                            "chat": {"id": 222, "first_name": "Aaron"},
                        },
                    },
                ],
            },
        )

    _install_transport(handler)
    r = await telegram_route.detect_chat(_FakeRequest())
    assert r["detected"] is True
    assert r["chat_id"] == "222"


@pytest.mark.asyncio
async def test_detect_chat_empty_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_token_only(monkeypatch)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": []})

    _install_transport(handler)
    r = await telegram_route.detect_chat(_FakeRequest())
    assert r["detected"] is False
    assert "message the bot" in r["error"].lower() or "message" in r["error"].lower()


@pytest.mark.asyncio
async def test_detect_chat_no_token_is_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await telegram_route.detect_chat(_FakeRequest())
    assert exc.value.status_code == 400


# ── test ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_test_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_creds(monkeypatch)

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/sendMessage")
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42}},
        )

    _install_transport(handler)
    r = await telegram_route.send_test_message(_FakeRequest())
    assert r["sent"] is True
    assert r["message_id"] == 42


@pytest.mark.asyncio
async def test_send_test_reports_api_error_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_creds(monkeypatch)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "description": "chat not found"},
        )

    _install_transport(handler)
    r = await telegram_route.send_test_message(_FakeRequest())
    assert r["sent"] is False
    assert "chat not found" in r["error"]


@pytest.mark.asyncio
async def test_send_test_no_creds_is_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await telegram_route.send_test_message(_FakeRequest())
    assert exc.value.status_code == 400
    # The error should name every missing key so the operator knows
    # exactly what to paste.
    detail = exc.value.detail
    assert "telegram_bot_token" in detail
    assert "telegram_chat_id" in detail
