"""Thin async HTTP client for the Arcads external API.

Arcads (https://external-api.arcads.ai) is the provider behind the
`ugc_video_agent`. Auth is HTTP Basic with the API key as username
and an empty password. We hit just three endpoints for v1 of the
agent — product list, video create, and asset/video poll — with
everything else left to a follow-up if the agent grows.

The client intentionally stays dumb: it builds requests, forwards
errors as an `ArcadsError`, and hands raw JSON back to the caller.
Shaping the response into a friendly dict is the tool handler's
job (``core/tools/builtin/arcads.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from core.logging import get_logger

log = get_logger("pilkd.integrations.arcads")

DEFAULT_BASE_URL = "https://external-api.arcads.ai"
DEFAULT_MODEL = "seedance-2.0"  # UGC selfie-style; supports audio + refs
DEFAULT_ASPECT_RATIO = "9:16"   # Reels / Shorts / TikTok default
DEFAULT_DURATION_S = 15


class ArcadsError(RuntimeError):
    """Wrap a non-2xx Arcads response. The status + body slice help the
    tool handler return a readable error to the chat thread."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"arcads api error {status}: {body[:200]}")
        self.status = status
        self.body = body


@dataclass
class ArcadsClient:
    """Low-level Arcads API client. One instance per tool call is fine —
    each method opens a short-lived httpx.AsyncClient so there's no
    lifetime management in the caller."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    timeout_s: float = 20.0

    def _auth(self) -> tuple[str, str]:
        # API key goes in the username slot; password stays empty. The
        # API also accepts a pre-encoded `Authorization: Basic ...`
        # header but letting httpx do it avoids a b64 miskey.
        return (self.api_key, "")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}{path}"
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.request(
                method,
                url,
                auth=self._auth(),
                json=json,
                headers={"Accept": "application/json"},
            )
        if resp.status_code >= 400:
            raise ArcadsError(resp.status_code, resp.text)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as e:
            raise ArcadsError(resp.status_code, resp.text) from e

    # ── products ────────────────────────────────────────────────────

    async def list_products(self) -> list[dict[str, Any]]:
        """GET /v1/products. Returns the raw array; the tool handler
        shapes it down to {id, name}."""
        body = await self._request("GET", "/v1/products")
        # Arcads returns either a bare list or a paginated {items: [...]}.
        # Accept both — schema isn't consistent across all list endpoints.
        if isinstance(body, list):
            return body
        if isinstance(body, dict) and isinstance(body.get("items"), list):
            return body["items"]
        return []

    # ── videos ──────────────────────────────────────────────────────

    async def create_video(
        self,
        *,
        product_id: str,
        prompt: str,
        model: str = DEFAULT_MODEL,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        duration_s: int | None = DEFAULT_DURATION_S,
        resolution: str | None = None,
        audio_enabled: bool | None = None,
        reference_images: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /v2/videos/generate — returns an asset object with an id
        the caller polls via `get_asset`. Seedance 2.0 is the default
        because it's the only model that accepts `audioEnabled`, which
        the UGC flow wants for lip-synced reads."""
        body: dict[str, Any] = {
            "model": model,
            "productId": product_id,
            "prompt": prompt,
            "aspectRatio": aspect_ratio,
        }
        if duration_s is not None:
            body["duration"] = duration_s
        if resolution is not None:
            body["resolution"] = resolution
        if audio_enabled is not None:
            body["audioEnabled"] = audio_enabled
        if reference_images:
            body["referenceImages"] = list(reference_images)
        return await self._request("POST", "/v2/videos/generate", json=body)

    async def get_asset(self, asset_id: str) -> dict[str, Any]:
        """GET /v1/assets/{id} — status enum is
        `created|pending|generated|failed|uploaded`. The video URL
        lives on the asset's `url` or on its `data` payload
        depending on the model; caller decides which to surface."""
        return await self._request("GET", f"/v1/assets/{asset_id}")
