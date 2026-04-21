"""Tests for the Telegram ↔ orchestrator chat bridge.

Stubs:
- ``httpx`` gets a ``MockTransport`` so Telegram API calls stay local.
- The orchestrator is a tiny fake that broadcasts ``chat.assistant``
  through a real Hub; this is the same contract the real orchestrator
  satisfies, so the bridge's subscribe → run → capture → reply flow
  gets exercised end-to-end.
"""

from __future__ import annotations

import asyncio
import json as _json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from core.api.hub import Hub
from core.integrations.telegram import TelegramConfig
from core.io.telegram_bridge import (
    DEFAULT_LONGPOLL_S,
    TelegramBridge,
    _read_offset,
    _write_offset,
)
from core.orchestrator.orchestrator import OrchestratorBusyError


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


def _ok(payload) -> dict:
    return {"ok": True, "result": payload}


class _FakeOrchestrator:
    """Minimal double for the real orchestrator.

    Exposes an async ``run`` that broadcasts ``chat.assistant`` through
    the hub — matching the real orchestrator's post-plan behavior —
    before returning. ``reply_text`` drives what the fake says back so
    each test can assert exact content.
    """

    def __init__(
        self,
        hub: Hub,
        *,
        reply_text: str = "hello operator",
        raise_exc: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.hub = hub
        self.reply_text = reply_text
        self.raise_exc = raise_exc
        self.delay = delay
        self.calls: list[str] = []

    async def run(self, goal: str) -> None:
        self.calls.append(goal)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_exc is not None:
            raise self.raise_exc
        await self.hub.broadcast(
            "chat.assistant",
            {"text": self.reply_text, "plan_id": "plan-test"},
        )


def _bridge(
    orchestrator,
    *,
    tmp_path: Path,
    hub: Hub | None = None,
    chat_id: str = "999",
) -> tuple[TelegramBridge, Hub]:
    h = hub or Hub()
    b = TelegramBridge(
        config=TelegramConfig(bot_token="tok-abc", chat_id=chat_id),
        orchestrator=orchestrator,  # type: ignore[arg-type]
        hub=h,
        state_path=tmp_path / "state" / "bridge.json",
        longpoll_seconds=DEFAULT_LONGPOLL_S,
    )
    return b, h


# ── dispatch (orchestrator subscribe → run → send back) ─────────


@pytest.mark.asyncio
async def test_dispatch_sends_orchestrator_reply(tmp_path: Path) -> None:
    """End-to-end happy path: an inbound message runs through the
    orchestrator and the broadcast assistant text lands back on
    Telegram as a sendMessage call."""
    sends: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/sendMessage"):
            sends.append(_json.loads(req.content.decode()))
            return httpx.Response(200, json=_ok({"message_id": 1}))
        raise AssertionError(f"unexpected url: {url}")

    _install_transport(handler)
    hub = Hub()
    orch = _FakeOrchestrator(hub, reply_text="reply-back")
    bridge, _ = _bridge(orch, tmp_path=tmp_path, hub=hub)
    await bridge._dispatch("hey PILK")
    assert orch.calls == ["hey PILK"]
    assert len(sends) == 1
    assert sends[0]["text"] == "reply-back"
    assert sends[0]["chat_id"] == "999"


@pytest.mark.asyncio
async def test_dispatch_empty_reply_falls_back(tmp_path: Path) -> None:
    sends: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/sendMessage"):
            sends.append(_json.loads(req.content.decode()))
            return httpx.Response(200, json=_ok({"message_id": 1}))
        raise AssertionError("unexpected url")

    _install_transport(handler)
    hub = Hub()
    orch = _FakeOrchestrator(hub, reply_text="")
    bridge, _ = _bridge(orch, tmp_path=tmp_path, hub=hub)
    await bridge._dispatch("ping")
    assert sends[0]["text"] == "(no response)"


@pytest.mark.asyncio
async def test_dispatch_busy_error_sends_retry_hint(tmp_path: Path) -> None:
    sends: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/sendMessage"):
            sends.append(_json.loads(req.content.decode()))
            return httpx.Response(200, json=_ok({"message_id": 1}))
        raise AssertionError("unexpected url")

    _install_transport(handler)
    hub = Hub()
    orch = _FakeOrchestrator(
        hub, raise_exc=OrchestratorBusyError("busy"),
    )
    bridge, _ = _bridge(orch, tmp_path=tmp_path, hub=hub)
    await bridge._dispatch("hey")
    assert "another task" in sends[0]["text"].lower()


@pytest.mark.asyncio
async def test_dispatch_unhandled_exception_replies_with_error(
    tmp_path: Path,
) -> None:
    sends: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/sendMessage"):
            sends.append(_json.loads(req.content.decode()))
            return httpx.Response(200, json=_ok({"message_id": 1}))
        raise AssertionError("unexpected url")

    _install_transport(handler)
    hub = Hub()
    orch = _FakeOrchestrator(
        hub, raise_exc=RuntimeError("boom"),
    )
    bridge, _ = _bridge(orch, tmp_path=tmp_path, hub=hub)
    await bridge._dispatch("hey")
    assert "went wrong" in sends[0]["text"].lower()
    assert "boom" in sends[0]["text"]


@pytest.mark.asyncio
async def test_dispatch_unsubscribes_listener(tmp_path: Path) -> None:
    """Every dispatch must unsubscribe its one-shot listener — a
    leaking subscription would cause subsequent chats to see stale
    captured state."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok({"message_id": 1}))

    _install_transport(handler)
    hub = Hub()
    orch = _FakeOrchestrator(hub, reply_text="a")
    bridge, _ = _bridge(orch, tmp_path=tmp_path, hub=hub)
    assert hub._listeners == []
    await bridge._dispatch("one")
    await bridge._dispatch("two")
    assert hub._listeners == []


# ── update handling (chat_id filter + non-text fallback) ─────────


@pytest.mark.asyncio
async def test_handle_update_ignores_foreign_chat(tmp_path: Path) -> None:
    sends: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/sendMessage"):
            sends.append(_json.loads(req.content.decode()))
        return httpx.Response(200, json=_ok({"message_id": 1}))

    _install_transport(handler)
    hub = Hub()
    orch = _FakeOrchestrator(hub)
    bridge, _ = _bridge(orch, tmp_path=tmp_path, hub=hub)
    await bridge._handle_update({
        "update_id": 1,
        "message": {
            "chat": {"id": 12345},  # not the configured chat_id
            "text": "hi",
        },
    })
    assert sends == []
    assert orch.calls == []


@pytest.mark.asyncio
async def test_handle_update_non_text_sends_gentle_reply(
    tmp_path: Path,
) -> None:
    sends: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/sendMessage"):
            sends.append(_json.loads(req.content.decode()))
        return httpx.Response(200, json=_ok({"message_id": 1}))

    _install_transport(handler)
    hub = Hub()
    orch = _FakeOrchestrator(hub)
    bridge, _ = _bridge(orch, tmp_path=tmp_path, hub=hub)
    # Photo message — no `text` field; the orchestrator must not run.
    await bridge._handle_update({
        "update_id": 2,
        "message": {
            "chat": {"id": 999},
            "photo": [{"file_id": "x"}],
        },
    })
    assert orch.calls == []
    assert len(sends) == 1
    assert "text" in sends[0]["text"].lower()


@pytest.mark.asyncio
async def test_handle_update_text_message_dispatches(
    tmp_path: Path,
) -> None:
    sends: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/sendMessage"):
            sends.append(_json.loads(req.content.decode()))
        return httpx.Response(200, json=_ok({"message_id": 1}))

    _install_transport(handler)
    hub = Hub()
    orch = _FakeOrchestrator(hub, reply_text="kicked off")
    bridge, _ = _bridge(orch, tmp_path=tmp_path, hub=hub)
    await bridge._handle_update({
        "update_id": 3,
        "message": {
            "chat": {"id": 999},
            "text": "  ingest gmail please  ",
        },
    })
    # Whitespace trimmed; goal passed through verbatim.
    assert orch.calls == ["ingest gmail please"]
    assert sends[-1]["text"] == "kicked off"


# ── offset persistence ──────────────────────────────────────────


def test_offset_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "state" / "bridge.json"
    assert _read_offset(p) is None
    _write_offset(p, 42)
    assert _read_offset(p) == 42


def test_offset_missing_file_is_none(tmp_path: Path) -> None:
    assert _read_offset(tmp_path / "never.json") is None


def test_offset_malformed_is_none(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("not json at all", encoding="utf-8")
    assert _read_offset(p) is None


# ── run loop advances offset ────────────────────────────────────


@pytest.mark.asyncio
async def test_run_loop_advances_offset_past_seen_updates(
    tmp_path: Path,
) -> None:
    """The bridge advances ``offset`` to ``last_update_id + 1`` after
    each batch, both in memory and on disk, so restarts don't replay
    old messages. We drive a single tick of the loop with a
    pre-signalled stop event so the loop exits after the first batch
    instead of polling forever."""
    state: dict[str, int] = {"calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/getUpdates"):
            state["calls"] += 1
            return httpx.Response(
                200,
                json=_ok([
                    {
                        "update_id": 100,
                        "message": {
                            "chat": {"id": 999},
                            "text": "first",
                        },
                    },
                    {
                        "update_id": 101,
                        "message": {
                            "chat": {"id": 999},
                            "text": "second",
                        },
                    },
                ]),
            )
        if url.endswith("/sendMessage"):
            return httpx.Response(200, json=_ok({"message_id": 1}))
        raise AssertionError(f"unexpected url: {url}")

    _install_transport(handler)
    hub = Hub()
    orch = _FakeOrchestrator(hub, reply_text="ok")
    bridge, _ = _bridge(orch, tmp_path=tmp_path, hub=hub)

    # Drive the loop and let the orchestrator fake trigger stop after
    # the second message has been processed. That guarantees the loop
    # finishes the in-flight batch and exits cleanly — we just want
    # to verify offset advances across a multi-update batch.
    original_run = orch.run

    async def run_and_stop_after_second(goal: str) -> None:
        await original_run(goal)
        if goal == "second":
            bridge._stop.set()

    orch.run = run_and_stop_after_second  # type: ignore[method-assign]
    await bridge._run()

    assert orch.calls == ["first", "second"]
    assert bridge._offset == 102
    persisted = _read_offset(tmp_path / "state" / "bridge.json")
    assert persisted == 102
