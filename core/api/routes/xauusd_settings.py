"""HTTP surface for XAU/USD runtime settings.

    GET  /xauusd/settings
        → {execution_mode: "approve"|"autonomous", updated_at: ...}

    PUT  /xauusd/settings/execution_mode
        body: {mode: "approve"|"autonomous"}
        → {execution_mode: ..., updated_at: ...}

The settings store is tiny and unencrypted — the values here aren't
secrets, they're operator toggles. They still travel browser→daemon
over the same bearer-auth surface every other protected route uses.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.logging import get_logger
from core.trading.xauusd.settings_store import (
    DEFAULT_EXECUTION_MODE,
    EXECUTION_MODES,
    XAUUSDSettingsStore,
)

log = get_logger("pilkd.xauusd_settings")

router = APIRouter(prefix="/xauusd/settings")


def _store(request: Request) -> XAUUSDSettingsStore:
    store = getattr(request.app.state, "xauusd_settings", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="xauusd_settings store offline"
        )
    return store


class SetModeBody(BaseModel):
    mode: str = Field(
        description="approve | autonomous",
        min_length=1,
        max_length=32,
    )


@router.get("")
async def get_settings(request: Request) -> dict:
    store = _store(request)
    current = None
    updated_at = None
    for entry in store.list_entries():
        if entry.name == "execution_mode":
            current = entry.value
            updated_at = entry.updated_at
            break
    return {
        "execution_mode": current or DEFAULT_EXECUTION_MODE,
        "is_default": current is None,
        "updated_at": updated_at,
        "allowed_modes": sorted(EXECUTION_MODES),
    }


@router.put("/execution_mode")
async def set_execution_mode_route(
    body: SetModeBody, request: Request
) -> dict:
    store = _store(request)
    mode = body.mode.strip().lower()
    if mode not in EXECUTION_MODES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown mode '{body.mode}'. "
                f"Allowed: {sorted(EXECUTION_MODES)}"
            ),
        )
    store.upsert("execution_mode", mode)
    log.info("xauusd_execution_mode_set", mode=mode)
    # Return the fresh row so the UI can display the updated timestamp
    # without a follow-up round trip.
    for entry in store.list_entries():
        if entry.name == "execution_mode":
            return {
                "execution_mode": entry.value,
                "updated_at": entry.updated_at,
            }
    return {"execution_mode": mode, "updated_at": None}
