"""HTTP surface for the user-managed integration-secrets store.

  GET    /integration-secrets               list configured keys (no values)
  PUT    /integration-secrets/{name}        body: {value: "..."} → upsert
  DELETE /integration-secrets/{name}        clear one key

Values only travel browser → daemon. Reads return ``configured: true/false``
plus an ``updated_at`` timestamp so the dashboard can show "set 3 days ago"
next to each field without ever re-revealing the actual token.

Two ways a secret name becomes "known":

* **Static entries** in :data:`KNOWN_SECRETS` — single-tenant integrations
  where one key exists per deploy (HubSpot, Hunter, Twelve Data, etc.).
  These always appear in the list-secrets response.

* **Pattern entries** in :data:`KNOWN_SECRET_PATTERNS` — families of
  names where we can't enumerate the exact slugs at build time
  (e.g. ``wordpress_<site>_app_password`` with one slot per client site).
  Any DB row whose name matches a pattern is surfaced in the list with
  a synthesized label; callers can also PUT new names matching a
  pattern and we'll accept them.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.logging import get_logger
from core.secrets import IntegrationSecretsStore

log = get_logger("pilkd.integration_secrets")

router = APIRouter(prefix="/integration-secrets")

# Static known integrations. Each entry becomes a row in the UI and
# each tool reads by the matching name. Add a new entry when adding
# a single-tenant integration; reach for a pattern when the slug is
# per-site / per-client / per-user.
KNOWN_SECRETS: dict[str, dict[str, str | None]] = {
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
    "nano_banana_api_key": {
        "label": "Nano Banana (Gemini 2.5 Flash Image)",
        "description": (
            "Google AI Studio API key for image generation. Get one at "
            "aistudio.google.com → Get API Key. The same key works as a "
            "generic Gemini key."
        ),
        "env": "NANO_BANANA_API_KEY",
    },
    "higgsfield_api_key": {
        "label": "Higgsfield (cinematic video gen)",
        "description": (
            "API key from cloud.higgsfield.ai → API Tokens. Used for "
            "text→video and image→video generations by the "
            "creative_content_agent."
        ),
        "env": "HIGGSFIELD_API_KEY",
    },
}


class _PatternDef:
    """One pattern entry: regex + metadata to synthesize a list row
    when a DB row matches."""

    def __init__(
        self,
        *,
        pattern: str,
        label_template: str,
        description: str,
    ) -> None:
        self.pattern = re.compile(pattern)
        self.label_template = label_template
        self.description = description

    def match(self, name: str) -> dict[str, str | None] | None:
        """Return synthesized metadata if ``name`` matches, else None.
        The label template can reference ``{slug}`` captured from the
        first group of the regex."""
        m = self.pattern.fullmatch(name)
        if m is None:
            return None
        slug = m.group(1) if m.groups() else name
        return {
            "label": self.label_template.format(slug=slug),
            "description": self.description,
            "env": None,
        }


# Pattern-based known secrets. Order is irrelevant — at most one
# pattern should match any given name.
KNOWN_SECRET_PATTERNS: list[_PatternDef] = [
    _PatternDef(
        pattern=r"wordpress_([a-z0-9][a-z0-9-]*)_app_password",
        label_template="WordPress ({slug})",
        description=(
            "Per-site WordPress credential in the form "
            "`username:app_password`. The app password comes from "
            "Users → Profile → Application Passwords on the target "
            "WordPress site. This key's ``{slug}`` should match the "
            "client's `wordpress_secret_key` in `clients/<slug>.yaml`."
        ),
    ),
]


def _match_pattern(name: str) -> dict[str, str | None] | None:
    for pat in KNOWN_SECRET_PATTERNS:
        meta = pat.match(name)
        if meta is not None:
            return meta
    return None


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
    if name in KNOWN_SECRETS:
        return
    if _match_pattern(name) is not None:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            f"unknown integration secret '{name}'. "
            f"Known: {sorted(KNOWN_SECRETS)} + patterns: "
            f"{[p.pattern.pattern for p in KNOWN_SECRET_PATTERNS]}"
        ),
    )


@router.get("")
async def list_secrets(request: Request) -> dict:
    """Return every known integration and whether it's configured.

    Static entries always appear. Pattern entries appear only when a
    matching row has been written — we can't enumerate all possible
    slugs, so "configured" names show up, unconfigured pattern slots
    don't.
    """
    store = _store(request)
    have = {e.name: e.updated_at for e in store.list_entries()}
    entries: list[dict[str, Any]] = [
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
    # Surface pattern-matched rows that exist in the DB. We can't list
    # ones that don't exist (the user has to know the slug to PUT a
    # new value), but the ones already saved should be visible.
    for db_name, updated_at in have.items():
        if db_name in KNOWN_SECRETS:
            continue
        meta = _match_pattern(db_name)
        if meta is None:
            # Row that matches neither static nor pattern — surface
            # with a neutral label so orphans are visible instead of
            # invisible.
            entries.append(
                {
                    "name": db_name,
                    "label": db_name,
                    "description": (
                        "Legacy / pattern-unmatched secret. Review + "
                        "delete if no longer used."
                    ),
                    "env": None,
                    "configured": True,
                    "updated_at": updated_at,
                }
            )
            continue
        entries.append(
            {
                "name": db_name,
                "label": meta["label"],
                "description": meta["description"],
                "env": meta["env"],
                "configured": True,
                "updated_at": updated_at,
            }
        )
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
