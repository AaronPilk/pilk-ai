"""Tests for POST /memory/distill — the auto-learning route that
asks an LLM to propose durable memory entries from recent plans.

No real network: the Anthropic client is stubbed. No real DB either;
PlanStore is in-memory-backed via a tiny protocol-satisfying fake.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.routes.memory import router as memory_router


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


class _FakeClient:
    """Minimum of anthropic.AsyncAnthropic that the distill route uses."""

    def __init__(self, reply_text: str) -> None:
        self._reply = reply_text
        self.calls: list[dict] = []

        outer = self

        class _Messages:
            async def create(self, **kwargs):
                outer.calls.append(kwargs)
                return _Response([_TextBlock(text=outer._reply)])

        self.messages = _Messages()


class _Response:
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class _FakePlans:
    """Tiny stand-in for PlanStore.list_plans."""

    def __init__(self, plans: list[dict]) -> None:
        self._plans = plans

    async def list_plans(self, limit: int = 50) -> list[dict]:
        return self._plans[:limit]


def _app(client_reply: str, plans: list[dict]) -> FastAPI:
    app = FastAPI()
    app.state.anthropic = _FakeClient(client_reply)
    app.state.plans = _FakePlans(plans)
    app.include_router(memory_router)
    return app


def test_distill_happy_path() -> None:
    plans = [
        {"goal": "send a short email to madison", "status": "completed"},
        {"goal": "no markdown headings please", "status": "completed"},
        {"goal": "save a quick xauusd snapshot", "status": "completed"},
    ]
    reply = (
        '{"proposals":[{"kind":"preference","title":"prefers short emails",'
        '"body":"emails should be brief","confidence":0.8,'
        '"rationale":"operator repeatedly asks for concise copy"}]}'
    )
    app = _app(reply, plans)
    r = TestClient(app).post("/memory/distill")
    assert r.status_code == 200
    body = r.json()
    assert body["window"] == 3
    assert len(body["proposals"]) == 1
    p = body["proposals"][0]
    assert p["kind"] == "preference"
    assert p["title"] == "prefers short emails"
    assert 0 <= p["confidence"] <= 1


def test_distill_accepts_fenced_json() -> None:
    """Haiku sometimes wraps JSON in a ```json fence. The parser has
    to strip that before decoding."""
    plans = [{"goal": "test", "status": "completed"}]
    reply = (
        "```json\n"
        '{"proposals":[{"kind":"fact","title":"lives in Tampa",'
        '"body":"","confidence":0.7}]}\n```'
    )
    app = _app(reply, plans)
    r = TestClient(app).post("/memory/distill")
    assert r.status_code == 200
    assert r.json()["proposals"][0]["title"] == "lives in Tampa"


def test_distill_empty_proposals_ok() -> None:
    plans = [{"goal": "say hi", "status": "completed"}]
    app = _app('{"proposals":[]}', plans)
    r = TestClient(app).post("/memory/distill")
    assert r.status_code == 200
    assert r.json()["proposals"] == []


def test_distill_sanitizes_bad_proposals() -> None:
    """Drop entries with invalid kind, missing title, etc."""
    plans = [{"goal": "x", "status": "completed"}]
    reply = (
        '{"proposals":['
        '{"kind":"bogus","title":"junk"},'           # bad kind
        '{"kind":"fact","title":""},'                 # no title
        '{"kind":"fact","title":"lives in Tampa"}'   # good
        "]}"
    )
    app = _app(reply, plans)
    r = TestClient(app).post("/memory/distill")
    proposals = r.json()["proposals"]
    assert len(proposals) == 1
    assert proposals[0]["title"] == "lives in Tampa"


def test_distill_503_when_client_missing() -> None:
    app = FastAPI()
    app.include_router(memory_router)  # no anthropic / no plans
    r = TestClient(app).post("/memory/distill")
    assert r.status_code == 503


def test_distill_502_when_llm_raises() -> None:
    class _Boom:
        class messages:  # noqa: N801 — mirror SDK
            @staticmethod
            async def create(**_kwargs):
                raise RuntimeError("rate limited")

    app = FastAPI()
    app.state.anthropic = _Boom()
    app.state.plans = _FakePlans([{"goal": "x", "status": "completed"}])
    app.include_router(memory_router)
    r = TestClient(app).post("/memory/distill")
    assert r.status_code == 502
    assert "rate limited" in r.json()["detail"]


def test_distill_window_param() -> None:
    plans = [{"goal": f"plan {i}", "status": "completed"} for i in range(40)]
    app = _app('{"proposals":[]}', plans)
    r = TestClient(app).post("/memory/distill", json={"window": 10})
    assert r.status_code == 200
    assert r.json()["window"] == 10
    # Confirm the LLM only saw 10 goals in its prompt.
    sent = app.state.anthropic.calls[0]["messages"][0]["content"]
    assert sent.count("[completed]") == 10


def test_distill_empty_history() -> None:
    """With no plans on record, skip the LLM call and return []."""
    app = _app('{"proposals":[]}', [])
    r = TestClient(app).post("/memory/distill")
    assert r.status_code == 200
    assert r.json() == {"proposals": [], "window": 0}
    # Did not invoke Anthropic at all.
    assert app.state.anthropic.calls == []
