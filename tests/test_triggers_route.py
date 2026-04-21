"""HTTP tests for the /triggers route.

Drives the route handlers directly with stubbed Request + app state —
same pattern as test_brain_route.py — so we exercise the exact JSON
shape the UI consumes without spinning up a full FastAPI app.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from fastapi import HTTPException

from core.api.routes import triggers as triggers_route
from core.config import get_settings
from core.db import ensure_schema
from core.triggers import TriggerRegistry


def _manifest(dir_: Path, name: str, body: str) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "manifest.yaml").write_text(dedent(body), encoding="utf-8")


async def _reg(tmp_path: Path) -> TriggerRegistry:
    settings = get_settings()
    ensure_schema(settings.db_path)
    _manifest(
        tmp_path / "daily",
        "daily",
        """
        name: daily
        agent_name: inbox_triage_agent
        goal: Triage inbox.
        schedule: { kind: cron, expression: "0 7 * * *" }
        """,
    )
    reg = TriggerRegistry(manifests_dir=tmp_path, db_path=settings.db_path)
    await reg.discover_and_install()
    return reg


class _FakeScheduler:
    def __init__(self) -> None:
        self.fires: list[str] = []
        self.raise_on_fire: Exception | None = None

    async def fire_now(self, name: str) -> dict[str, Any]:
        if self.raise_on_fire is not None:
            raise self.raise_on_fire
        self.fires.append(name)
        return {"status": "fired", "fired_at": "2026-04-21T12:00:00+00:00"}


@dataclass
class _FakeState:
    triggers: TriggerRegistry | None = None
    trigger_scheduler: _FakeScheduler | None = None


class _FakeApp:
    def __init__(
        self,
        triggers: TriggerRegistry | None = None,
        scheduler: _FakeScheduler | None = None,
    ) -> None:
        self.state = _FakeState(triggers=triggers, trigger_scheduler=scheduler)


@dataclass
class _FakeRequest:
    app: _FakeApp = field(default_factory=_FakeApp)


# ── list ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_empty_when_registry_missing() -> None:
    resp = await triggers_route.list_triggers(_FakeRequest())
    assert resp == {"triggers": []}


@pytest.mark.asyncio
async def test_list_returns_registered_triggers(tmp_path: Path) -> None:
    reg = await _reg(tmp_path)
    req = _FakeRequest(_FakeApp(triggers=reg))
    resp = await triggers_route.list_triggers(req)
    assert len(resp["triggers"]) == 1
    row = resp["triggers"][0]
    assert row["name"] == "daily"
    assert row["schedule"]["expression"] == "0 7 * * *"


# ── enable / disable ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enable_marks_trigger_active(tmp_path: Path) -> None:
    reg = await _reg(tmp_path)
    await reg.set_enabled("daily", False)
    req = _FakeRequest(_FakeApp(triggers=reg))
    resp = await triggers_route.enable_trigger("daily", req)
    assert resp == {"name": "daily", "enabled": True}
    assert reg.enabled("daily") is True


@pytest.mark.asyncio
async def test_disable_marks_trigger_inactive(tmp_path: Path) -> None:
    reg = await _reg(tmp_path)
    req = _FakeRequest(_FakeApp(triggers=reg))
    resp = await triggers_route.disable_trigger("daily", req)
    assert resp == {"name": "daily", "enabled": False}
    assert reg.enabled("daily") is False


@pytest.mark.asyncio
async def test_enable_unknown_trigger_returns_404(tmp_path: Path) -> None:
    reg = await _reg(tmp_path)
    req = _FakeRequest(_FakeApp(triggers=reg))
    with pytest.raises(HTTPException) as info:
        await triggers_route.enable_trigger("ghost", req)
    assert info.value.status_code == 404


@pytest.mark.asyncio
async def test_enable_without_registry_returns_503() -> None:
    req = _FakeRequest()
    with pytest.raises(HTTPException) as info:
        await triggers_route.enable_trigger("daily", req)
    assert info.value.status_code == 503


# ── fire ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fire_proxies_to_scheduler(tmp_path: Path) -> None:
    reg = await _reg(tmp_path)
    scheduler = _FakeScheduler()
    req = _FakeRequest(_FakeApp(triggers=reg, scheduler=scheduler))
    resp = await triggers_route.fire_trigger("daily", req)
    assert resp["name"] == "daily"
    assert resp["status"] == "fired"
    assert scheduler.fires == ["daily"]


@pytest.mark.asyncio
async def test_fire_without_scheduler_returns_503(tmp_path: Path) -> None:
    reg = await _reg(tmp_path)
    req = _FakeRequest(_FakeApp(triggers=reg))
    with pytest.raises(HTTPException) as info:
        await triggers_route.fire_trigger("daily", req)
    assert info.value.status_code == 503
