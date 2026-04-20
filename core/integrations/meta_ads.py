"""Meta Marketing API client — thin httpx wrapper for the ads agent.

Covers exactly the surface :mod:`core.tools.builtin.meta_ads` needs:

    Campaigns:        list / create / patch-status / update-budget
    Ad sets:          list / create / patch-status / update-budget
    Ads:              list / create / patch-status
    Ad creatives:     create
    Asset uploads:    image (multipart) / video (multipart)
    Insights:         GET with arbitrary level + date preset

Stays deliberately small — no clever caching, no OAuth refresh dance,
no async rate-limit backoff. Meta's long-lived user tokens last ~60
days; the operator rotates them in Settings → API Keys when they
expire. If upstream returns anything non-2xx we raise
:class:`MetaAdsError` with the upstream ``error.message`` so the tool
handler can surface it cleanly.

Docs: https://developers.facebook.com/docs/marketing-api
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from core.logging import get_logger

log = get_logger("pilkd.meta_ads")

GRAPH_BASE = "https://graph.facebook.com"
DEFAULT_VERSION = "v21.0"
DEFAULT_TIMEOUT_S = 30.0


class MetaAdsError(Exception):
    """Upstream Meta API returned non-2xx."""

    def __init__(self, status: int, message: str, raw: Any = None):
        super().__init__(f"Meta Ads API {status}: {message}")
        self.status = status
        self.message = message
        self.raw = raw


@dataclass(frozen=True)
class MetaAdsConfig:
    access_token: str
    ad_account_id: str  # without the `act_` prefix; we add it where needed
    page_id: str | None = None
    api_version: str = DEFAULT_VERSION

    @property
    def account_node(self) -> str:
        aid = self.ad_account_id.strip()
        return aid if aid.startswith("act_") else f"act_{aid}"


def _raise(resp: httpx.Response) -> None:
    if resp.is_success:
        return
    try:
        body = resp.json()
        err = body.get("error") or {}
        message = str(err.get("message") or resp.text[:200])
        raise MetaAdsError(status=resp.status_code, message=message, raw=body)
    except ValueError:
        raise MetaAdsError(
            status=resp.status_code,
            message=resp.text[:200] or f"HTTP {resp.status_code}",
        ) from None


class MetaAdsClient:
    def __init__(self, config: MetaAdsConfig, *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._cfg = config
        self._timeout = timeout

    def _url(self, path: str) -> str:
        return f"{GRAPH_BASE}/{self._cfg.api_version}/{path.lstrip('/')}"

    def _auth_params(self, extra: dict | None = None) -> dict:
        p: dict = {"access_token": self._cfg.access_token}
        if extra:
            p.update(extra)
        return p

    # ── Reads ──────────────────────────────────────────────────

    async def list_campaigns(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        params: dict = {
            "fields": "id,name,objective,status,effective_status,"
                      "daily_budget,lifetime_budget,created_time",
            "limit": limit,
        }
        if status:
            params["effective_status"] = f'["{status}"]'
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(
                self._url(f"{self._cfg.account_node}/campaigns"),
                params=self._auth_params(params),
            )
        _raise(r)
        return list(r.json().get("data") or [])

    async def list_adsets(
        self, *, campaign_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        params: dict = {
            "fields": "id,name,campaign_id,status,effective_status,"
                      "daily_budget,lifetime_budget,optimization_goal,"
                      "billing_event",
            "limit": limit,
        }
        node = (
            f"{campaign_id}/adsets"
            if campaign_id
            else f"{self._cfg.account_node}/adsets"
        )
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(
                self._url(node),
                params=self._auth_params(params),
            )
        _raise(r)
        return list(r.json().get("data") or [])

    async def list_ads(
        self, *, adset_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        params: dict = {
            "fields": "id,name,adset_id,status,effective_status,creative",
            "limit": limit,
        }
        node = (
            f"{adset_id}/ads"
            if adset_id
            else f"{self._cfg.account_node}/ads"
        )
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(
                self._url(node),
                params=self._auth_params(params),
            )
        _raise(r)
        return list(r.json().get("data") or [])

    async def get_insights(
        self,
        object_id: str,
        *,
        level: str = "ad",
        date_preset: str = "last_7d",
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        default_fields = [
            "impressions", "clicks", "spend", "ctr", "cpc", "cpm",
            "reach", "frequency", "conversions", "actions", "cost_per_action_type",
        ]
        params: dict = {
            "level": level,
            "date_preset": date_preset,
            "fields": ",".join(fields or default_fields),
        }
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(
                self._url(f"{object_id}/insights"),
                params=self._auth_params(params),
            )
        _raise(r)
        return list(r.json().get("data") or [])

    # ── Creates (always PAUSED; operator flips status separately) ──

    async def create_campaign(
        self,
        *,
        name: str,
        objective: str,
        special_ad_categories: list[str] | None = None,
        daily_budget: int | None = None,
        lifetime_budget: int | None = None,
    ) -> dict[str, Any]:
        data: dict = {
            "name": name,
            "objective": objective,
            "status": "PAUSED",
            "special_ad_categories": special_ad_categories or [],
        }
        if daily_budget is not None:
            data["daily_budget"] = daily_budget
        if lifetime_budget is not None:
            data["lifetime_budget"] = lifetime_budget
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(
                self._url(f"{self._cfg.account_node}/campaigns"),
                params=self._auth_params(),
                data=data,
            )
        _raise(r)
        return r.json()

    async def create_adset(
        self,
        *,
        campaign_id: str,
        name: str,
        optimization_goal: str,
        billing_event: str,
        daily_budget: int | None = None,
        lifetime_budget: int | None = None,
        bid_amount: int | None = None,
        targeting: dict | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, Any]:
        data: dict = {
            "name": name,
            "campaign_id": campaign_id,
            "optimization_goal": optimization_goal,
            "billing_event": billing_event,
            "status": "PAUSED",
            "targeting": targeting or {"geo_locations": {"countries": ["US"]}},
        }
        if daily_budget is not None:
            data["daily_budget"] = daily_budget
        if lifetime_budget is not None:
            data["lifetime_budget"] = lifetime_budget
        if bid_amount is not None:
            data["bid_amount"] = bid_amount
        if start_time:
            data["start_time"] = start_time
        if end_time:
            data["end_time"] = end_time
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(
                self._url(f"{self._cfg.account_node}/adsets"),
                params=self._auth_params(),
                data=data,
            )
        _raise(r)
        return r.json()

    async def create_ad(
        self,
        *,
        adset_id: str,
        name: str,
        creative_id: str,
    ) -> dict[str, Any]:
        data: dict = {
            "name": name,
            "adset_id": adset_id,
            "creative": f'{{"creative_id":"{creative_id}"}}',
            "status": "PAUSED",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(
                self._url(f"{self._cfg.account_node}/ads"),
                params=self._auth_params(),
                data=data,
            )
        _raise(r)
        return r.json()

    async def create_creative(
        self,
        *,
        name: str,
        page_id: str | None = None,
        image_hash: str | None = None,
        video_id: str | None = None,
        message: str | None = None,
        link: str | None = None,
        headline: str | None = None,
        description: str | None = None,
        call_to_action_type: str | None = None,
    ) -> dict[str, Any]:
        pg = page_id or self._cfg.page_id
        if not pg:
            raise MetaAdsError(
                status=400,
                message=(
                    "page_id required — pass one or set meta_page_id "
                    "in Settings → API Keys."
                ),
            )
        link_data: dict = {}
        if message:
            link_data["message"] = message
        if link:
            link_data["link"] = link
        if headline:
            link_data["name"] = headline
        if description:
            link_data["description"] = description
        if image_hash:
            link_data["image_hash"] = image_hash
        if call_to_action_type and link:
            link_data["call_to_action"] = {
                "type": call_to_action_type,
                "value": {"link": link},
            }
        object_story_spec: dict = {"page_id": pg}
        if video_id:
            object_story_spec["video_data"] = {
                "video_id": video_id,
                **({"message": message} if message else {}),
                **({"call_to_action": link_data.get("call_to_action")}
                   if link_data.get("call_to_action") else {}),
            }
        else:
            object_story_spec["link_data"] = link_data
        import json as _json
        data: dict = {
            "name": name,
            "object_story_spec": _json.dumps(object_story_spec),
        }
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(
                self._url(f"{self._cfg.account_node}/adcreatives"),
                params=self._auth_params(),
                data=data,
            )
        _raise(r)
        return r.json()

    # ── Uploads ────────────────────────────────────────────────

    async def upload_image(self, path: Path) -> dict[str, Any]:
        """Uploads an image; returns the {hash, ...} payload Meta
        references in `link_data.image_hash` on an ad creative."""
        p = Path(path)
        if not p.is_file():
            raise MetaAdsError(status=400, message=f"file not found: {p}")
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            with p.open("rb") as fh:
                files = {"source": (p.name, fh, "application/octet-stream")}
                r = await c.post(
                    self._url(f"{self._cfg.account_node}/adimages"),
                    params=self._auth_params(),
                    files=files,
                )
        _raise(r)
        payload = r.json()
        images = payload.get("images") or {}
        # Meta keys the result by filename; return the first hash.
        for _fname, meta in images.items():
            return {"hash": meta.get("hash"), "url": meta.get("url")}
        return payload

    async def upload_video(self, path: Path, *, title: str | None = None) -> dict[str, Any]:
        """Uploads a video via the non-resumable /advideos endpoint.
        Returns the {id, ...} payload Meta references in the ad
        creative as `object_story_spec.video_data.video_id`."""
        p = Path(path)
        if not p.is_file():
            raise MetaAdsError(status=400, message=f"file not found: {p}")
        data: dict = {}
        if title:
            data["title"] = title
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            with p.open("rb") as fh:
                files = {"source": (p.name, fh, "application/octet-stream")}
                r = await c.post(
                    self._url(f"{self._cfg.account_node}/advideos"),
                    params=self._auth_params(),
                    data=data,
                    files=files,
                )
        _raise(r)
        return r.json()

    # ── Status + budget changes ────────────────────────────────

    async def set_status(
        self, object_id: str, status: str
    ) -> dict[str, Any]:
        status = status.upper()
        if status not in {"ACTIVE", "PAUSED", "ARCHIVED", "DELETED"}:
            raise MetaAdsError(
                status=400,
                message=f"status must be ACTIVE|PAUSED|ARCHIVED|DELETED, got {status}",
            )
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(
                self._url(object_id),
                params=self._auth_params(),
                data={"status": status},
            )
        _raise(r)
        return r.json()

    async def update_budget(
        self,
        object_id: str,
        *,
        daily_budget: int | None = None,
        lifetime_budget: int | None = None,
    ) -> dict[str, Any]:
        data: dict = {}
        if daily_budget is not None:
            data["daily_budget"] = daily_budget
        if lifetime_budget is not None:
            data["lifetime_budget"] = lifetime_budget
        if not data:
            raise MetaAdsError(
                status=400,
                message="pass daily_budget or lifetime_budget",
            )
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(
                self._url(object_id),
                params=self._auth_params(),
                data=data,
            )
        _raise(r)
        return r.json()


__all__ = ["MetaAdsClient", "MetaAdsConfig", "MetaAdsError"]
