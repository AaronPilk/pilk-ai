"""Integrations HTTP surface.

  GET /integrations/status   which external accounts are currently linked

For now this just covers Google; add entries here as more integrations
arrive (Slack, Stripe, etc.).
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from core.config import get_settings
from core.integrations.google import google_status

router = APIRouter(prefix="/integrations")


@router.get("/status")
async def integrations_status(request: Request) -> dict:
    settings = get_settings()
    google = google_status(settings.google_credentials_path)
    return {
        "google": google.to_public(),
    }
