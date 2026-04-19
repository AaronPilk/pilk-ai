"""HTTP surface for the user-managed integration-secrets store.

  GET    /integration-secrets               list configured keys (no values)
  PUT    /integration-secrets/{name}        body: {value: "..."} → upsert
  DELETE /integration-secrets/{name}        clear one key

Values only travel browser → daemon. Reads return ``configured: true/false``
plus an ``updated_at`` timestamp so the dashboard can show "set 3 days ago"
next to each field without ever re-revealing the actual token.

The set of ``name`` values is validated against a known-integrations list
so typos don't pile up ghost rows. Adding a new integration is a one-line
addition to ``KNOWN_SECRETS`` below.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.logging import get_logger
from core.secrets import IntegrationSecretsStore

log = get_logger("pilkd.integration_secrets")

router = APIRouter(prefix="/integration-secrets")

# Known integrations that can be set via the dashboard. Keep this list
# short and curated: every entry becomes a row in the UI, and each tool
# reads by the matching name in `_secret(name, fallback)`.
KNOWN_SECRETS: dict[str, dict[str, str]] = {
    "hubspot_private_token": {
        "label": "HubSpot",
        "description": (
            "Private App token (Settings → Integrations → Private Apps "
            "in HubSpot). Needs CRM contact + note scopes."
        ),
        "env": "HUBSPOT_PRIVATE_TOKEN",
    },
    "hunter_io_api_key": {
        "label": "Hunter.io",
        "description": (
            "API key from hunter.io → API section. Used to enrich "
            "domains with emails."
        ),
        "env": "HUNTER_IO_API_KEY",
    },
    "google_places_api_key": {
        "label": "Google Places",
        "description": (
            "Google Cloud API key with Places API (New) enabled. The "
            "same key works for PageSpeed if both APIs are enabled on "
            "the project."
        ),
        "env": "GOOGLE_PLACES_API_KEY",
    },
    "pagespeed_api_key": {
        "label": "PageSpeed Insights",
        "description": (
            "Google Cloud API key with PageSpeed Insights API enabled. "
            "Feel free to reuse the Places key above."
        ),
        "env": "PAGESPEED_API_KEY",
    },
    "twelvedata_api_key": {
        "label": "Twelve Data (XAU/USD feed)",
        "description": (
            "Price feed for the XAU/USD execution agent. Free tier at "
            "twelvedata.com → Dashboard → API Keys. 8 req/min limit."
        ),
        "env": "TWELVEDATA_API_KEY",
    },
}


class SetBody(BaseModel):
    value: str = Field(
        min_length=1,
        max_length=8000,
        description="Raw API token/key. Transits over HTTPS + bearer auth.",
    )


def _store(request: Request) -> IntegrationSecretsStore:
    store = getattr(request.app.state, "integration_secrets", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="integration_secrets store offline"
        )
    return store


def _ensure_known(name: str) -> None:
    if name not in KNOWN_SECRETS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown integration secret '{name}'. "
                f"Known: {sorted(KNOWN_SECRETS)}"
            ),
        )


@router.get("")
async def list_secrets(request: Request) -> dict:
    """Return every known integration and whether it's configured.

    Never echoes stored values. The dashboard uses `configured` + the
    per-entry metadata to decide between "Add key" and "Replace key".
    """
    store = _store(request)
    have = {e.name: e.updated_at for e in store.list_entries()}
    entries = [
        {
            "name": name,
            "label": meta["label"],
            "description": meta["description"],
            "env": meta["env"],
            "configured": name in have,
            "updated_at": have.get(name),
        }
        for name, meta in KNOWN_SECRETS.items()
    ]
    return {"entries": entries}


@router.put("/{name}")
async def set_secret(name: str, body: SetBody, request: Request) -> dict:
    _ensure_known(name)
    store = _store(request)
    try:
        store.upsert(name, body.value.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    log.info("integration_secret_upserted", name=name)
    return {"name": name, "configured": True}


@router.delete("/{name}")
async def delete_secret(name: str, request: Request) -> dict:
    _ensure_known(name)
    store = _store(request)
    removed = store.delete(name)
    log.info("integration_secret_deleted", name=name, existed=removed)
    return {"name": name, "configured": False, "removed": removed}
