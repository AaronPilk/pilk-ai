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
import contextlib
import json as _json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from core.api.hub import Hub
from core.brain import Vault
from core.integrations.telegram import TelegramConfig
from core.io.telegram_bridge import (
    CHAT_LOG_FOLDER,
    DEFAULT_LONGPOLL_S,
    SESSION_LOG_FOLDER,
    TelegramBridge,
    _ChatSession,
    _compose_prompt,
    _read_state,
    _write_state,
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
    vault: Vault | None = None,
    coalesce_window_s: float = 0.0,
    busy_retry_budget_s: float = 0.0,
) -> tuple[TelegramBridge, Hub]:
    h = hub or Hub()
    b = TelegramBridge(
        config=TelegramConfig(bot_token="tok-abc", chat_id=chat_id),
        orchestrator=orchestrator,  # type: ignore[arg-type]
        hub=h,
        state_path=tmp_path / "state" / "bridge.json",
        longpoll_seconds=DEFAULT_LONGPOLL_S,
        vault=vault,
        coalesce_window_s=coalesce_window_s,
        busy_retry_budget_s=busy_retry_budget_s,
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
    """When the orchestrator stays busy past the retry budget, the
    operator gets a clear 'try again' message instead of the call
    being silently dropped."""
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
    # busy_retry_budget_s=0 means one try then give up — keeps the
    # test instant even though real prod retries for ~45s.
    bridge, _ = _bridge(
        orch, tmp_path=tmp_path, hub=hub, busy_retry_budget_s=0.0
    )
    await bridge._dispatch("hey")
    assert "still finishing" in sends[0]["text"].lower()


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
async def test_handle_update_text_message_enqueues(
    tmp_path: Path,
) -> None:
    """Inbound text messages are enqueued for the processor rather
    than dispatched inline. Trimming still happens at the boundary so
    downstream code never sees leading/trailing whitespace."""

    def handler(_req: httpx.Request) -> httpx.Response:
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
    assert orch.calls == []  # processor not running; still enqueued.
    assert bridge._queue.qsize() == 1
    assert bridge._queue.get_nowait() == "ingest gmail please"


# ── bridge state persistence ────────────────────────────────────


def test_state_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "state" / "bridge.json"
    assert _read_state(p) == {}
    _write_state(p, {"offset": 42, "session": {"id": "tg-test"}})
    state = _read_state(p)
    assert state["offset"] == 42
    assert state["session"]["id"] == "tg-test"


def test_state_missing_file_is_empty(tmp_path: Path) -> None:
    assert _read_state(tmp_path / "never.json") == {}


def test_state_malformed_is_empty(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("not json at all", encoding="utf-8")
    assert _read_state(p) == {}


def test_state_back_compat_offset_only(tmp_path: Path) -> None:
    """Pre-session state files only had ``{"offset": N}``. Loading one
    must not crash and session state should fall back to fresh init."""
    p = tmp_path / "legacy.json"
    p.write_text(_json.dumps({"offset": 77}), encoding="utf-8")
    state = _read_state(p)
    assert state.get("offset") == 77
    assert "session" not in state


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

    # Stop the loop as soon as it hands the second update to
    # _handle_update — the offset must have been written by then.
    original_handle = bridge._handle_update

    async def handle_and_stop_after_second(upd):
        await original_handle(upd)
        if (upd.get("message") or {}).get("text") == "second":
            bridge._stop.set()

    bridge._handle_update = (  # type: ignore[method-assign]
        handle_and_stop_after_second
    )
    await bridge._run()

    # Messages land on the queue (the processor runs separately via
    # start()). The loop's contract is just "advance offset on every
    # update you touch."
    assert bridge._queue.qsize() == 2
    assert bridge._offset == 102
    persisted = _read_state(tmp_path / "state" / "bridge.json")
    assert persisted.get("offset") == 102


# ── rolling conversation history ─────────────────────────────────


@pytest.mark.asyncio
async def test_second_dispatch_sees_first_as_history(
    tmp_path: Path,
) -> None:
    """PILK must see prior turns in the preamble on the second
    dispatch — that's the whole fix for the 'fresh conversation'
    Telegram bug."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok({"message_id": 1}))

    _install_transport(handler)
    hub = Hub()
    orch = _FakeOrchestrator(hub, reply_text="reply-one")
    bridge, _ = _bridge(orch, tmp_path=tmp_path, hub=hub)
    await bridge._dispatch("first message")
    orch.reply_text = "reply-two"
    await bridge._dispatch("follow-up")

    # First call: no preamble — history was empty.
    assert orch.calls[0] == "first message"
    # Second call: preamble carries the previous exchange so PILK
    # can continue the thread instead of re-greeting.
    assert "first message" in orch.calls[1]
    assert "reply-one" in orch.calls[1]
    assert orch.calls[1].endswith("follow-up")


@pytest.mark.asyncio
async def test_history_window_is_bounded(tmp_path: Path) -> None:
    """The rolling window caps at ``HISTORY_MAX_TURNS`` so long
    threads don't blow the token budget."""
    from core.io.telegram_bridge import HISTORY_MAX_TURNS

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok({"message_id": 1}))

    _install_transport(handler)
    hub = Hub()
    orch = _FakeOrchestrator(hub, reply_text="ack")
    bridge, _ = _bridge(orch, tmp_path=tmp_path, hub=hub)
    for i in range(HISTORY_MAX_TURNS + 5):
        await bridge._dispatch(f"msg-{i}")
    # Each turn is 2 entries (user + assistant).
    assert len(bridge._history) == HISTORY_MAX_TURNS * 2


@pytest.mark.asyncio
async def test_history_survives_daemon_restart(tmp_path: Path) -> None:
    """A restart mid-conversation must not wipe PILK's short-term
    memory: the second bridge's deque has to rehydrate from disk and
    see the first bridge's exchanges."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok({"message_id": 1}))

    _install_transport(handler)
    hub_a = Hub()
    orch_a = _FakeOrchestrator(hub_a, reply_text="first-reply")
    bridge_a, _ = _bridge(orch_a, tmp_path=tmp_path, hub=hub_a)
    await bridge_a._dispatch("do you remember this line?")

    # Simulate a daemon restart — fresh bridge, same state path, same
    # everything else. The history deque should rehydrate from disk
    # before the next dispatch so the preamble includes the prior turn.
    hub_b = Hub()
    orch_b = _FakeOrchestrator(hub_b, reply_text="still here")
    bridge_b, _ = _bridge(orch_b, tmp_path=tmp_path, hub=hub_b)
    await bridge_b.start()
    try:
        assert len(bridge_b._history) == 2
        await bridge_b._dispatch("do you?")
    finally:
        await bridge_b.stop()

    # On the second dispatch the preamble must carry the prior turn —
    # that's the entire point of persisting history.
    assert "do you remember this line?" in orch_b.calls[-1]
    assert "first-reply" in orch_b.calls[-1]


def test_state_history_truncates_oversized_turns(tmp_path: Path) -> None:
    """Entries larger than ``HISTORY_TURN_CHAR_CAP`` get truncated on
    the way to disk so the state file can't grow unbounded when the
    operator pastes a 50 KB message mid-thread."""
    from core.io.telegram_bridge import HISTORY_TURN_CHAR_CAP

    _install_transport(lambda _: httpx.Response(200, json=_ok({})))
    hub = Hub()
    orch = _FakeOrchestrator(hub, reply_text="ack")
    bridge, _ = _bridge(orch, tmp_path=tmp_path, hub=hub)
    big = "x" * (HISTORY_TURN_CHAR_CAP + 500)
    bridge._history.append(("user", big))
    bridge._history.append(("assistant", "ok"))
    bridge._save_state()
    state = _read_state(bridge._state_path)
    persisted = state["history"]
    assert persisted[0]["role"] == "user"
    assert len(persisted[0]["text"]) == HISTORY_TURN_CHAR_CAP
    assert persisted[1]["text"] == "ok"


# ── coalesce window ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_processor_coalesces_rapid_messages(
    tmp_path: Path,
) -> None:
    """Two messages landing inside the coalesce window must merge
    into one orchestrator call instead of two separate greets."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok({"message_id": 1}))

    _install_transport(handler)
    hub = Hub()
    orch = _FakeOrchestrator(hub, reply_text="merged-reply")
    bridge, _ = _bridge(
        orch, tmp_path=tmp_path, hub=hub, coalesce_window_s=0.2
    )
    await bridge._queue.put("hello")
    await bridge._queue.put("and another thing")
    task = asyncio.create_task(bridge._process_queue())
    # Give the processor just enough time to pull both and dispatch.
    await asyncio.sleep(0.5)
    bridge._stop.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task

    assert len(orch.calls) == 1
    assert "hello" in orch.calls[0]
    assert "and another thing" in orch.calls[0]


@pytest.mark.asyncio
async def test_processor_does_not_coalesce_across_windows(
    tmp_path: Path,
) -> None:
    """A message arriving well AFTER the first dispatch finishes
    must be its own turn, not merged with a stale batch."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok({"message_id": 1}))

    _install_transport(handler)
    hub = Hub()
    orch = _FakeOrchestrator(hub, reply_text="ok")
    bridge, _ = _bridge(
        orch, tmp_path=tmp_path, hub=hub, coalesce_window_s=0.05
    )
    task = asyncio.create_task(bridge._process_queue())
    await bridge._queue.put("first")
    await asyncio.sleep(0.25)  # well past the coalesce window
    await bridge._queue.put("second")
    await asyncio.sleep(0.25)
    bridge._stop.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task

    assert len(orch.calls) == 2
    # History preamble guarantees the second call carries the first.
    assert orch.calls[0] == "first"
    assert orch.calls[1].endswith("second")


# ── busy retry ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_busy_retry_recovers_within_budget(
    tmp_path: Path,
) -> None:
    """When the orchestrator is busy on the first attempt but frees
    up before the retry budget expires, the message goes through
    instead of being dropped."""
    sends: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/sendMessage"):
            sends.append(_json.loads(req.content.decode()))
        return httpx.Response(200, json=_ok({"message_id": 1}))

    _install_transport(handler)
    hub = Hub()

    class _FlipOrchestrator(_FakeOrchestrator):
        def __init__(self, hub: Hub) -> None:
            super().__init__(hub, reply_text="finally")
            self._rejected = 0

        async def run(self, goal: str) -> None:
            self.calls.append(goal)
            if self._rejected < 2:
                self._rejected += 1
                raise OrchestratorBusyError("still busy")
            await self.hub.broadcast(
                "chat.assistant",
                {"text": self.reply_text, "plan_id": "p"},
            )

    orch = _FlipOrchestrator(hub)
    bridge, _ = _bridge(
        orch, tmp_path=tmp_path, hub=hub, busy_retry_budget_s=5.0
    )
    await bridge._dispatch("eventually works")
    assert len(orch.calls) == 3  # 2 busy + 1 success
    assert sends[-1]["text"] == "finally"


# ── vault auto-ingest ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_appends_exchange_to_vault(
    tmp_path: Path,
) -> None:
    """Every successful turn must land in the daily Telegram chat
    log so the operator has a full searchable record without having
    to say 'remember this'."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok({"message_id": 1}))

    _install_transport(handler)
    vault_root = tmp_path / "vault"
    vault = Vault(vault_root)
    vault.ensure_initialized()
    hub = Hub()
    orch = _FakeOrchestrator(hub, reply_text="noted")
    bridge, _ = _bridge(orch, tmp_path=tmp_path, hub=hub, vault=vault)

    await bridge._dispatch("remember this bit")
    await bridge._dispatch("and this one too")

    # One dated file under chats/telegram/ with both exchanges
    # appended, ordered chronologically.
    chat_dir = vault_root / CHAT_LOG_FOLDER
    files = list(chat_dir.iterdir())
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "remember this bit" in body
    assert "and this one too" in body
    # Both PILK replies are there too — so search-across-vault finds
    # either side of the conversation.
    assert body.count("**PILK:** noted") == 2


@pytest.mark.asyncio
async def test_dispatch_without_vault_is_silent(
    tmp_path: Path,
) -> None:
    """The auto-ingest step must silent-fail when no vault is wired
    — operators without the brain configured still get a working
    chat bridge."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok({"message_id": 1}))

    _install_transport(handler)
    hub = Hub()
    orch = _FakeOrchestrator(hub, reply_text="ok")
    bridge, _ = _bridge(orch, tmp_path=tmp_path, hub=hub, vault=None)
    # Should NOT raise.
    await bridge._dispatch("hi")


# ── _compose_prompt ──────────────────────────────────────────────


def test_compose_prompt_empty_history_passes_through() -> None:
    assert _compose_prompt([], "hello") == "hello"


def test_compose_prompt_labels_turns() -> None:
    prompt = _compose_prompt(
        [
            ("user", "what's up"),
            ("assistant", "helping you out"),
        ],
        "add detail",
    )
    assert "Me: what's up" in prompt
    assert "PILK: helping you out" in prompt
    assert prompt.rstrip().endswith("add detail")


def test_compose_prompt_truncates_overlong_turns() -> None:
    from core.io.telegram_bridge import HISTORY_TURN_CHAR_CAP

    big = "x" * (HISTORY_TURN_CHAR_CAP + 500)
    prompt = _compose_prompt([("user", big)], "short")
    assert "[truncated]" in prompt
    assert len(prompt) < 2 * HISTORY_TURN_CHAR_CAP


# ── session boundary tracking ────────────────────────────────────


def test_chat_session_first_tick_opens_new_session() -> None:
    from datetime import UTC, datetime

    s = _ChatSession(idle_gap_s=900.0)
    sid, is_new = s.tick(datetime(2026, 4, 22, 13, 3, 0, tzinfo=UTC))
    assert is_new is True
    assert sid.startswith("tg-20260422-1303")


def test_chat_session_within_gap_stays_same() -> None:
    from datetime import UTC, datetime, timedelta

    s = _ChatSession(idle_gap_s=900.0)
    t0 = datetime(2026, 4, 22, 13, 0, 0, tzinfo=UTC)
    sid_a, new_a = s.tick(t0)
    sid_b, new_b = s.tick(t0 + timedelta(minutes=10))
    assert new_a is True and new_b is False
    assert sid_a == sid_b


def test_chat_session_after_gap_opens_new() -> None:
    from datetime import UTC, datetime, timedelta

    s = _ChatSession(idle_gap_s=900.0)
    t0 = datetime(2026, 4, 22, 13, 0, 0, tzinfo=UTC)
    sid_a, _ = s.tick(t0)
    sid_b, new_b = s.tick(t0 + timedelta(minutes=20))
    assert new_b is True
    assert sid_a != sid_b


def test_chat_session_state_round_trip() -> None:
    from datetime import UTC, datetime

    s = _ChatSession(idle_gap_s=900.0)
    s.tick(datetime(2026, 4, 22, 13, 0, 0, tzinfo=UTC))
    state = s.as_state()
    s2 = _ChatSession(idle_gap_s=900.0)
    s2.load_state(state)
    assert s2.session_id == s.session_id
    assert s2.started_at == s.started_at


@pytest.mark.asyncio
async def test_dispatch_writes_per_session_vault_file(
    tmp_path: Path,
) -> None:
    """Every dispatch writes an append to a per-session file under
    ``sessions/telegram/``. Two dispatches within the idle gap both
    land in the same file, keeping a conversation together."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok({"message_id": 1}))

    _install_transport(handler)
    vault_root = tmp_path / "vault"
    vault = Vault(vault_root)
    vault.ensure_initialized()
    hub = Hub()
    orch = _FakeOrchestrator(hub, reply_text="noted")
    bridge, _ = _bridge(orch, tmp_path=tmp_path, hub=hub, vault=vault)

    await bridge._dispatch("msg one")
    await bridge._dispatch("msg two")

    session_dir = vault_root / SESSION_LOG_FOLDER
    files = list(session_dir.iterdir())
    assert len(files) == 1, f"expected one session file, got {files!r}"
    body = files[0].read_text(encoding="utf-8")
    # Header written once, on session open.
    assert body.count("# Session ") == 1
    assert "Channel: Telegram" in body
    # Both exchanges persisted under the session header.
    assert "msg one" in body and "msg two" in body
    assert body.count("**PILK:** noted") == 2
