"""Google Ads REST client — thin httpx wrapper for the ads agent.

Covers exactly the surface :mod:`core.tools.builtin.google_ads` needs
to run a Search campaign end-to-end:

    Reports:        searchStream (arbitrary GAQL queries)
    Campaigns:      list / create / patch-status / update-budget
    Ad groups:      list / create / patch-status
    Ads:            list / create (responsive search)
    Keywords:       add / add-negative
    Budgets:        create (shared campaign-budget resources)

Auth is the standard Google Ads OAuth triplet — client_id +
client_secret + long-lived refresh_token mint a short-lived access
token on demand, cached in-memory for ~50 min. Every call carries
``developer-token`` + optional ``login-customer-id`` headers alongside
the bearer token.

We keep this small and deliberate:

* **REST, not gRPC.** The official client is 40MB of protobufs we
  don't need — the Ads REST API covers every mutation we care about
  for Search campaigns and costs zero extra deps beyond httpx.
* **GAQL builder is minimal.** We let the caller pass a raw query
  string (the tool shapes it) rather than building a DSL.
* **PAUSED-by-default creates** mirror the Meta client — activation
  is a separate FINANCIAL call on the tool side, never inline here.

Docs: https://developers.google.com/google-ads/api/rest/overview
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from core.logging import get_logger

log = get_logger("pilkd.google_ads")

ADS_API_BASE = "https://googleads.googleapis.com"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_VERSION = "v21"
DEFAULT_TIMEOUT_S = 30.0
# Google access tokens live 60 minutes; we refresh at 50 to avoid a
# mid-request expiry. Tuned-for-robustness, not for token thrift.
ACCESS_TOKEN_SKEW_S = 10 * 60


class GoogleAdsError(Exception):
    """Upstream Google Ads (or OAuth) returned non-2xx."""

    def __init__(self, status: int, message: str, raw: Any = None):
        super().__init__(f"Google Ads API {status}: {message}")
        self.status = status
        self.message = message
        self.raw = raw


@dataclass
class GoogleAdsConfig:
    developer_token: str
    client_id: str
    client_secret: str
    refresh_token: str
    customer_id: str  # digits only, no dashes
    login_customer_id: str | None = None
    api_version: str = DEFAULT_VERSION

    @property
    def customer_resource(self) -> str:
        return f"customers/{self.customer_id.replace('-', '').strip()}"


@dataclass
class _AccessToken:
    token: str
    expires_at: float = 0.0

    def is_fresh(self) -> bool:
        # 30-second floor so we don't issue with a dying token.
        return self.token and self.expires_at > time.time() + 30


@dataclass
class GoogleAdsClient:
    config: GoogleAdsConfig
    timeout: float = DEFAULT_TIMEOUT_S
    _access: _AccessToken = field(
        default_factory=lambda: _AccessToken(token="", expires_at=0.0)
    )

    async def _ensure_access_token(self) -> str:
        if self._access.is_fresh():
            return self._access.token
        data = {
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "refresh_token": self.config.refresh_token,
            "grant_type": "refresh_token",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(OAUTH_TOKEN_URL, data=data)
        if not r.is_success:
            try:
                body = r.json()
                message = str(
                    body.get("error_description")
                    or body.get("error")
                    or r.text[:200]
                )
            except ValueError:
                body = None
                message = r.text[:200] or f"HTTP {r.status_code}"
            raise GoogleAdsError(
                status=r.status_code,
                message=f"OAuth refresh failed: {message}",
                raw=body,
            )
        payload = r.json()
        access = payload.get("access_token")
        expires_in = int(payload.get("expires_in") or 0)
        if not access:
            raise GoogleAdsError(
                status=500,
                message="OAuth refresh returned no access_token",
                raw=payload,
            )
        self._access = _AccessToken(
            token=access,
            expires_at=time.time() + max(0, expires_in - ACCESS_TOKEN_SKEW_S),
        )
        return access

    async def _headers(self) -> dict[str, str]:
        token = await self._ensure_access_token()
        h = {
            "Authorization": f"Bearer {token}",
            "developer-token": self.config.developer_token,
            "Content-Type": "application/json",
        }
        if self.config.login_customer_id:
            h["login-customer-id"] = self.config.login_customer_id.replace(
                "-", ""
            ).strip()
        return h

    def _url(self, path: str) -> str:
        v = self.config.api_version
        return f"{ADS_API_BASE}/{v}/{path.lstrip('/')}"

    async def _post(
        self, path: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(self._url(path), headers=headers, json=body)
        if not r.is_success:
            _raise_ads_error(r)
        return r.json()

    # ── Reports (searchStream) ─────────────────────────────────

    async def search(
        self,
        query: str,
        *,
        validate_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Run a GAQL query against the target customer. Returns the
        flat list of row dicts concatenated across all streamed chunks.
        The caller shapes the query — we just pass it through."""
        path = f"{self.config.customer_resource}/googleAds:searchStream"
        body: dict[str, Any] = {"query": query}
        if validate_only:
            body["validateOnly"] = True
        payload = await self._post(path, body)
        chunks = payload if isinstance(payload, list) else [payload]
        rows: list[dict[str, Any]] = []
        for chunk in chunks:
            for r in chunk.get("results") or []:
                rows.append(r)
        return rows

    # ── Budgets ────────────────────────────────────────────────

    async def create_budget(
        self,
        *,
        name: str,
        amount_micros: int,
        delivery_method: str = "STANDARD",
        explicitly_shared: bool = False,
    ) -> dict[str, Any]:
        path = f"{self.config.customer_resource}/campaignBudgets:mutate"
        body = {
            "operations": [
                {
                    "create": {
                        "name": name,
                        "amountMicros": str(int(amount_micros)),
                        "deliveryMethod": delivery_method,
                        "explicitlyShared": bool(explicitly_shared),
                    }
                }
            ]
        }
        return await self._post(path, body)

    # ── Campaigns ──────────────────────────────────────────────

    async def create_campaign(
        self,
        *,
        name: str,
        budget_resource: str,
        advertising_channel_type: str = "SEARCH",
        bidding_strategy_type: str = "MANUAL_CPC",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        path = f"{self.config.customer_resource}/campaigns:mutate"
        spec: dict[str, Any] = {
            "name": name,
            "status": "PAUSED",
            "advertisingChannelType": advertising_channel_type,
            "campaignBudget": budget_resource,
        }
        # Set a canonical bid-strategy field based on the requested
        # strategy type. We deliberately keep the allowed set narrow
        # for V1 — Manual CPC + the two automated strategies cover
        # nearly every real search campaign we'll build.
        if bidding_strategy_type == "MANUAL_CPC":
            spec["manualCpc"] = {"enhancedCpcEnabled": False}
        elif bidding_strategy_type == "MAXIMIZE_CONVERSIONS":
            spec["maximizeConversions"] = {}
        elif bidding_strategy_type == "TARGET_SPEND":
            spec["targetSpend"] = {}
        else:
            raise GoogleAdsError(
                status=400,
                message=(
                    "bidding_strategy_type must be MANUAL_CPC | "
                    "MAXIMIZE_CONVERSIONS | TARGET_SPEND; got "
                    f"{bidding_strategy_type}"
                ),
            )
        if start_date:
            spec["startDate"] = start_date
        if end_date:
            spec["endDate"] = end_date
        body = {"operations": [{"create": spec}]}
        return await self._post(path, body)

    async def update_campaign_budget(
        self, budget_resource: str, *, amount_micros: int
    ) -> dict[str, Any]:
        path = f"{self.config.customer_resource}/campaignBudgets:mutate"
        body = {
            "operations": [
                {
                    "update": {
                        "resourceName": budget_resource,
                        "amountMicros": str(int(amount_micros)),
                    },
                    "updateMask": "amount_micros",
                }
            ]
        }
        return await self._post(path, body)

    async def set_campaign_status(
        self, campaign_resource: str, status: str
    ) -> dict[str, Any]:
        status = status.upper()
        if status not in {"ENABLED", "PAUSED", "REMOVED"}:
            raise GoogleAdsError(
                status=400,
                message=f"status must be ENABLED|PAUSED|REMOVED; got {status}",
            )
        path = f"{self.config.customer_resource}/campaigns:mutate"
        body = {
            "operations": [
                {
                    "update": {
                        "resourceName": campaign_resource,
                        "status": status,
                    },
                    "updateMask": "status",
                }
            ]
        }
        return await self._post(path, body)

    # ── Ad groups ──────────────────────────────────────────────

    async def create_ad_group(
        self,
        *,
        name: str,
        campaign_resource: str,
        cpc_bid_micros: int | None = None,
        ad_group_type: str = "SEARCH_STANDARD",
    ) -> dict[str, Any]:
        path = f"{self.config.customer_resource}/adGroups:mutate"
        spec: dict[str, Any] = {
            "name": name,
            "campaign": campaign_resource,
            "status": "PAUSED",
            "type": ad_group_type,
        }
        if cpc_bid_micros is not None:
            spec["cpcBidMicros"] = str(int(cpc_bid_micros))
        body = {"operations": [{"create": spec}]}
        return await self._post(path, body)

    # ── Keywords ───────────────────────────────────────────────

    async def add_keywords(
        self,
        ad_group_resource: str,
        keywords: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Keywords: list of {text, match_type} where match_type is
        EXACT | PHRASE | BROAD. Every entry becomes one ad-group
        criterion."""
        path = f"{self.config.customer_resource}/adGroupCriteria:mutate"
        ops: list[dict] = []
        for kw in keywords:
            text = (kw.get("text") or "").strip()
            match = (kw.get("match_type") or "BROAD").upper()
            if match not in {"EXACT", "PHRASE", "BROAD"}:
                raise GoogleAdsError(
                    status=400,
                    message=f"match_type must be EXACT|PHRASE|BROAD; got {match}",
                )
            if not text:
                raise GoogleAdsError(
                    status=400,
                    message="each keyword must have non-empty text",
                )
            ops.append(
                {
                    "create": {
                        "adGroup": ad_group_resource,
                        "status": "ENABLED",
                        "keyword": {"text": text, "matchType": match},
                    }
                }
            )
        return await self._post(path, {"operations": ops})

    async def add_negative_keywords(
        self,
        campaign_resource: str,
        keywords: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Campaign-level negative keywords. Same {text, match_type}
        shape as add_keywords; ``negative: true`` is the campaign-
        criterion flag."""
        path = f"{self.config.customer_resource}/campaignCriteria:mutate"
        ops: list[dict] = []
        for kw in keywords:
            text = (kw.get("text") or "").strip()
            match = (kw.get("match_type") or "BROAD").upper()
            if not text:
                continue
            ops.append(
                {
                    "create": {
                        "campaign": campaign_resource,
                        "negative": True,
                        "keyword": {"text": text, "matchType": match},
                    }
                }
            )
        if not ops:
            raise GoogleAdsError(
                status=400,
                message="no valid negative keywords supplied",
            )
        return await self._post(path, {"operations": ops})

    # ── Ads (responsive search) ────────────────────────────────

    async def create_responsive_search_ad(
        self,
        *,
        ad_group_resource: str,
        headlines: list[str],
        descriptions: list[str],
        final_urls: list[str],
        path1: str | None = None,
        path2: str | None = None,
    ) -> dict[str, Any]:
        if len(headlines) < 3 or len(headlines) > 15:
            raise GoogleAdsError(
                status=400,
                message=(
                    f"responsive search ads take 3-15 headlines; got "
                    f"{len(headlines)}"
                ),
            )
        if len(descriptions) < 2 or len(descriptions) > 4:
            raise GoogleAdsError(
                status=400,
                message=(
                    f"responsive search ads take 2-4 descriptions; got "
                    f"{len(descriptions)}"
                ),
            )
        if not final_urls:
            raise GoogleAdsError(
                status=400,
                message="at least one final_url is required",
            )
        rsa: dict[str, Any] = {
            "headlines": [{"text": h} for h in headlines],
            "descriptions": [{"text": d} for d in descriptions],
        }
        if path1:
            rsa["path1"] = path1
        if path2:
            rsa["path2"] = path2
        path = f"{self.config.customer_resource}/adGroupAds:mutate"
        body = {
            "operations": [
                {
                    "create": {
                        "adGroup": ad_group_resource,
                        "status": "PAUSED",
                        "ad": {
                            "finalUrls": list(final_urls),
                            "responsiveSearchAd": rsa,
                        },
                    }
                }
            ]
        }
        return await self._post(path, body)


def _raise_ads_error(resp: httpx.Response) -> None:
    """Unpack a Google Ads error payload into :class:`GoogleAdsError`.

    Google's error body is nested — the first entry in
    ``error.details[*].errors`` carries the most actionable message;
    we prefer that over the outer ``error.message`` when present.
    """
    try:
        body = resp.json()
    except ValueError:
        raise GoogleAdsError(
            status=resp.status_code,
            message=resp.text[:200] or f"HTTP {resp.status_code}",
        ) from None
    err = body.get("error") or {}
    deep = None
    for det in err.get("details") or []:
        for inner in det.get("errors") or []:
            msg = inner.get("message")
            if msg:
                deep = str(msg)
                break
        if deep:
            break
    message = deep or str(err.get("message") or json.dumps(body)[:200])
    raise GoogleAdsError(status=resp.status_code, message=message, raw=body)


__all__ = ["GoogleAdsClient", "GoogleAdsConfig", "GoogleAdsError"]
