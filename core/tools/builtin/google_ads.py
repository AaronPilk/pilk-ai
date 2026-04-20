"""Google Ads tools for the google_ads_agent — full operator, search
campaigns end-to-end.

Twelve tools, risk classes assigned per real-world impact (identical
framing to the Meta Ads surface so PILK can treat the two platforms
as interchangeable):

    NET_READ     list_campaigns / list_ad_groups / list_ads /
                 get_metrics / run_gaql
    NET_WRITE    create_budget / create_campaign / create_ad_group /
                 add_keywords / add_negative_keywords /
                 create_responsive_search_ad
    FINANCIAL    set_campaign_status (ENABLED spends money) and
                 update_campaign_budget

Every handler reads credentials via ``resolve_secret`` so the operator
pastes them in Settings → API Keys rather than editing env vars.
Missing config returns a clean ``is_error`` outcome telling the agent
which keys to set.
"""

from __future__ import annotations

from typing import Any

from core.config import get_settings
from core.integrations.google_ads import (
    GoogleAdsClient,
    GoogleAdsConfig,
    GoogleAdsError,
)
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.secrets import resolve_secret
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.tools.google_ads")


# ── Client builder ───────────────────────────────────────────────


def _client() -> GoogleAdsClient | tuple[None, str]:
    s = get_settings()
    required = {
        "google_ads_developer_token": s.google_ads_developer_token,
        "google_ads_client_id": s.google_ads_client_id,
        "google_ads_client_secret": s.google_ads_client_secret,
        "google_ads_refresh_token": s.google_ads_refresh_token,
        "google_ads_customer_id": s.google_ads_customer_id,
    }
    resolved: dict[str, str | None] = {
        k: resolve_secret(k, v) for k, v in required.items()
    }
    missing = [k for k, v in resolved.items() if not v]
    if missing:
        return (
            None,
            "Google Ads not configured. Missing in Settings → API "
            f"Keys: {', '.join(sorted(missing))}.",
        )
    login_customer_id = resolve_secret(
        "google_ads_login_customer_id", s.google_ads_login_customer_id
    )
    cfg = GoogleAdsConfig(
        developer_token=resolved["google_ads_developer_token"],  # type: ignore[arg-type]
        client_id=resolved["google_ads_client_id"],  # type: ignore[arg-type]
        client_secret=resolved["google_ads_client_secret"],  # type: ignore[arg-type]
        refresh_token=resolved["google_ads_refresh_token"],  # type: ignore[arg-type]
        customer_id=resolved["google_ads_customer_id"],  # type: ignore[arg-type]
        login_customer_id=login_customer_id,
    )
    return GoogleAdsClient(config=cfg)


def _unwrap(client_or_err):
    if isinstance(client_or_err, tuple):
        return None, ToolOutcome(content=client_or_err[1], is_error=True)
    return client_or_err, None


def _surface(e: GoogleAdsError) -> ToolOutcome:
    return ToolOutcome(
        content=f"Google Ads {e.status}: {e.message}",
        is_error=True,
        data={"status": e.status, "raw": e.raw},
    )


# ── Reads ────────────────────────────────────────────────────────


async def _list_campaigns(args: dict, _ctx: ToolContext) -> ToolOutcome:
    client, err = _unwrap(_client())
    if err:
        return err
    limit = int(args.get("limit") or 50)
    status = str(args.get("status") or "").strip().upper()
    query = (
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign.advertising_channel_type, campaign.bidding_strategy_type, "
        "campaign_budget.amount_micros "
        "FROM campaign "
    )
    if status:
        query += f"WHERE campaign.status = '{status}' "
    query += f"ORDER BY campaign.id DESC LIMIT {limit}"
    try:
        rows = await client.search(query)
    except GoogleAdsError as e:
        return _surface(e)
    flat = [_flatten_campaign_row(r) for r in rows]
    lines = [
        f"- {c['name']} (id={c['id']}, {c['status']}, "
        f"type={c['channel_type']}, budget=${c['daily_budget_usd']}/d)"
        for c in flat
    ] or ["(no campaigns)"]
    return ToolOutcome(
        content=f"{len(flat)} campaign(s):\n" + "\n".join(lines),
        data={"campaigns": flat},
    )


google_ads_list_campaigns_tool = Tool(
    name="google_ads_list_campaigns",
    description=(
        "List campaigns on the operator's Google Ads account. Optional "
        "`status` filters to one of ENABLED | PAUSED | REMOVED. "
        "Returns id, name, status, channel type, bidding strategy, "
        "daily budget."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        },
    },
    risk=RiskClass.NET_READ,
    handler=_list_campaigns,
)


async def _list_ad_groups(args: dict, _ctx: ToolContext) -> ToolOutcome:
    client, err = _unwrap(_client())
    if err:
        return err
    campaign_id = str(args.get("campaign_id") or "").strip()
    limit = int(args.get("limit") or 50)
    query = (
        "SELECT ad_group.id, ad_group.name, ad_group.status, "
        "ad_group.type, ad_group.cpc_bid_micros, campaign.id "
        "FROM ad_group "
    )
    if campaign_id:
        query += f"WHERE campaign.id = {int(campaign_id)} "
    query += f"ORDER BY ad_group.id DESC LIMIT {limit}"
    try:
        rows = await client.search(query)
    except GoogleAdsError as e:
        return _surface(e)
    flat = [
        {
            "id": _get(r, "adGroup.id"),
            "name": _get(r, "adGroup.name"),
            "status": _get(r, "adGroup.status"),
            "type": _get(r, "adGroup.type"),
            "cpc_bid_micros": _get(r, "adGroup.cpcBidMicros"),
            "campaign_id": _get(r, "campaign.id"),
        }
        for r in rows
    ]
    lines = [
        f"- {g['name']} (id={g['id']}, {g['status']}, campaign={g['campaign_id']})"
        for g in flat
    ] or ["(no ad groups)"]
    return ToolOutcome(
        content=f"{len(flat)} ad group(s):\n" + "\n".join(lines),
        data={"ad_groups": flat},
    )


google_ads_list_ad_groups_tool = Tool(
    name="google_ads_list_ad_groups",
    description=(
        "List ad groups. Pass `campaign_id` to scope to one campaign; "
        "omit for every ad group on the account."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "campaign_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        },
    },
    risk=RiskClass.NET_READ,
    handler=_list_ad_groups,
)


async def _list_ads(args: dict, _ctx: ToolContext) -> ToolOutcome:
    client, err = _unwrap(_client())
    if err:
        return err
    ad_group_id = str(args.get("ad_group_id") or "").strip()
    limit = int(args.get("limit") or 50)
    query = (
        "SELECT ad_group_ad.ad.id, ad_group_ad.status, "
        "ad_group_ad.ad.final_urls, ad_group_ad.ad.type, "
        "ad_group.id "
        "FROM ad_group_ad "
    )
    if ad_group_id:
        query += f"WHERE ad_group.id = {int(ad_group_id)} "
    query += f"ORDER BY ad_group_ad.ad.id DESC LIMIT {limit}"
    try:
        rows = await client.search(query)
    except GoogleAdsError as e:
        return _surface(e)
    flat = [
        {
            "ad_id": _get(r, "adGroupAd.ad.id"),
            "status": _get(r, "adGroupAd.status"),
            "ad_type": _get(r, "adGroupAd.ad.type"),
            "final_urls": _get(r, "adGroupAd.ad.finalUrls") or [],
            "ad_group_id": _get(r, "adGroup.id"),
        }
        for r in rows
    ]
    return ToolOutcome(
        content=f"{len(flat)} ad(s).",
        data={"ads": flat},
    )


google_ads_list_ads_tool = Tool(
    name="google_ads_list_ads",
    description=(
        "List ads. Pass `ad_group_id` to scope; omit for every ad on "
        "the account."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "ad_group_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        },
    },
    risk=RiskClass.NET_READ,
    handler=_list_ads,
)


async def _get_metrics(args: dict, _ctx: ToolContext) -> ToolOutcome:
    """Run a canned campaign/ad_group/ad-level metrics query over a
    date range. Ad-level detail available when ``level=AD``; otherwise
    aggregates at the requested level."""
    client, err = _unwrap(_client())
    if err:
        return err
    level = str(args.get("level") or "CAMPAIGN").upper()
    date_range = str(args.get("date_range") or "LAST_7_DAYS").upper()
    if level not in {"CAMPAIGN", "AD_GROUP", "AD"}:
        return ToolOutcome(
            content="level must be CAMPAIGN | AD_GROUP | AD",
            is_error=True,
        )
    from_resource = {
        "CAMPAIGN": "campaign",
        "AD_GROUP": "ad_group",
        "AD": "ad_group_ad",
    }[level]
    selects = (
        "metrics.impressions, metrics.clicks, metrics.cost_micros, "
        "metrics.ctr, metrics.average_cpc, metrics.conversions, "
        "metrics.cost_per_conversion"
    )
    name_fields = {
        "CAMPAIGN": "campaign.id, campaign.name, ",
        "AD_GROUP": "ad_group.id, ad_group.name, ",
        "AD": "ad_group_ad.ad.id, ",
    }[level]
    query = (
        f"SELECT {name_fields}{selects} "
        f"FROM {from_resource} "
        f"WHERE segments.date DURING {date_range}"
    )
    try:
        rows = await client.search(query)
    except GoogleAdsError as e:
        return _surface(e)
    return ToolOutcome(
        content=(
            f"{len(rows)} metric row(s) at {level} level over "
            f"{date_range}."
        ),
        data={"level": level, "date_range": date_range, "rows": rows},
    )


google_ads_get_metrics_tool = Tool(
    name="google_ads_get_metrics",
    description=(
        "Fetch performance metrics for campaigns / ad groups / ads "
        "over a Google-Ads preset date range. Returns impressions, "
        "clicks, cost_micros, ctr, avg CPC, conversions, cost per "
        "conversion."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "level": {
                "type": "string",
                "enum": ["CAMPAIGN", "AD_GROUP", "AD"],
            },
            "date_range": {
                "type": "string",
                "description": (
                    "Google Ads date preset: TODAY, YESTERDAY, "
                    "LAST_7_DAYS, LAST_14_DAYS, LAST_30_DAYS, "
                    "THIS_MONTH, LAST_MONTH, ALL_TIME."
                ),
            },
        },
    },
    risk=RiskClass.NET_READ,
    handler=_get_metrics,
)


async def _run_gaql(args: dict, _ctx: ToolContext) -> ToolOutcome:
    query = str(args.get("query") or "").strip()
    if not query or not query.upper().startswith("SELECT"):
        return ToolOutcome(
            content="run_gaql requires a non-empty SELECT query.",
            is_error=True,
        )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        rows = await client.search(query)
    except GoogleAdsError as e:
        return _surface(e)
    return ToolOutcome(
        content=f"{len(rows)} row(s) returned.",
        data={"rows": rows},
    )


google_ads_run_gaql_tool = Tool(
    name="google_ads_run_gaql",
    description=(
        "Escape-hatch for arbitrary Google Ads Query Language "
        "(GAQL) SELECT queries when the canned read tools don't "
        "cover what the agent needs. Read-only — no mutations."
    ),
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    risk=RiskClass.NET_READ,
    handler=_run_gaql,
)


# ── Creates (NET_WRITE; campaigns always PAUSED) ─────────────────


async def _create_budget(args: dict, _ctx: ToolContext) -> ToolOutcome:
    name = str(args.get("name") or "").strip()
    daily_usd = args.get("daily_usd")
    if not name or daily_usd is None:
        return ToolOutcome(
            content="google_ads_create_budget requires 'name' and "
                    "'daily_usd'.",
            is_error=True,
        )
    try:
        daily_usd_f = float(daily_usd)
    except (TypeError, ValueError):
        return ToolOutcome(
            content="daily_usd must be a number.", is_error=True,
        )
    if daily_usd_f <= 0:
        return ToolOutcome(
            content="daily_usd must be positive.", is_error=True,
        )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.create_budget(
            name=name,
            amount_micros=int(daily_usd_f * 1_000_000),
            delivery_method=str(
                args.get("delivery_method") or "STANDARD"
            ).upper(),
            explicitly_shared=bool(args.get("explicitly_shared") or False),
        )
    except GoogleAdsError as e:
        return _surface(e)
    results = payload.get("results") or []
    resource = (results[0] or {}).get("resourceName") if results else None
    return ToolOutcome(
        content=(
            f"Created budget '{name}' at ${daily_usd_f:.2f}/day "
            f"(resource={resource})."
        ),
        data={"resource_name": resource, "raw": payload},
    )


google_ads_create_budget_tool = Tool(
    name="google_ads_create_budget",
    description=(
        "Create a campaign budget resource (shared-budget toggle "
        "available). Budgets exist independently of campaigns and are "
        "referenced by resourceName — feed that into "
        "google_ads_create_campaign as budget_resource."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "daily_usd": {"type": "number"},
            "delivery_method": {
                "type": "string",
                "enum": ["STANDARD", "ACCELERATED"],
            },
            "explicitly_shared": {"type": "boolean"},
        },
        "required": ["name", "daily_usd"],
    },
    risk=RiskClass.NET_WRITE,
    handler=_create_budget,
)


async def _create_campaign(args: dict, _ctx: ToolContext) -> ToolOutcome:
    name = str(args.get("name") or "").strip()
    budget_resource = str(args.get("budget_resource") or "").strip()
    if not name or not budget_resource:
        return ToolOutcome(
            content="google_ads_create_campaign requires 'name' and "
                    "'budget_resource' (from google_ads_create_budget).",
            is_error=True,
        )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.create_campaign(
            name=name,
            budget_resource=budget_resource,
            advertising_channel_type=str(
                args.get("advertising_channel_type") or "SEARCH"
            ).upper(),
            bidding_strategy_type=str(
                args.get("bidding_strategy_type") or "MANUAL_CPC"
            ).upper(),
            start_date=args.get("start_date"),
            end_date=args.get("end_date"),
        )
    except GoogleAdsError as e:
        return _surface(e)
    results = payload.get("results") or []
    resource = (results[0] or {}).get("resourceName") if results else None
    return ToolOutcome(
        content=(
            f"Created campaign '{name}' (resource={resource}) in "
            "PAUSED status. Activate with google_ads_set_status when "
            "ready — that's FINANCIAL."
        ),
        data={"resource_name": resource, "raw": payload},
    )


google_ads_create_campaign_tool = Tool(
    name="google_ads_create_campaign",
    description=(
        "Create a Google Ads campaign. Always PAUSED — activation is "
        "a separate FINANCIAL call. Channel defaults to SEARCH. Bid "
        "strategy: MANUAL_CPC | MAXIMIZE_CONVERSIONS | TARGET_SPEND."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "budget_resource": {"type": "string"},
            "advertising_channel_type": {
                "type": "string",
                "enum": [
                    "SEARCH", "DISPLAY", "PERFORMANCE_MAX", "VIDEO",
                    "SHOPPING",
                ],
            },
            "bidding_strategy_type": {
                "type": "string",
                "enum": [
                    "MANUAL_CPC", "MAXIMIZE_CONVERSIONS", "TARGET_SPEND",
                ],
            },
            "start_date": {
                "type": "string",
                "description": "YYYY-MM-DD.",
            },
            "end_date": {
                "type": "string",
                "description": "YYYY-MM-DD.",
            },
        },
        "required": ["name", "budget_resource"],
    },
    risk=RiskClass.NET_WRITE,
    handler=_create_campaign,
)


async def _create_ad_group(args: dict, _ctx: ToolContext) -> ToolOutcome:
    name = str(args.get("name") or "").strip()
    campaign_resource = str(args.get("campaign_resource") or "").strip()
    if not name or not campaign_resource:
        return ToolOutcome(
            content="google_ads_create_ad_group requires 'name' and "
                    "'campaign_resource'.",
            is_error=True,
        )
    cpc_bid_micros: int | None = None
    if args.get("cpc_bid_usd") is not None:
        try:
            cpc_bid_micros = int(float(args["cpc_bid_usd"]) * 1_000_000)
        except (TypeError, ValueError):
            return ToolOutcome(
                content="cpc_bid_usd must be a number.", is_error=True,
            )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.create_ad_group(
            name=name,
            campaign_resource=campaign_resource,
            cpc_bid_micros=cpc_bid_micros,
            ad_group_type=str(
                args.get("ad_group_type") or "SEARCH_STANDARD"
            ).upper(),
        )
    except GoogleAdsError as e:
        return _surface(e)
    results = payload.get("results") or []
    resource = (results[0] or {}).get("resourceName") if results else None
    return ToolOutcome(
        content=(
            f"Created ad group '{name}' (resource={resource}) in "
            "PAUSED status."
        ),
        data={"resource_name": resource, "raw": payload},
    )


google_ads_create_ad_group_tool = Tool(
    name="google_ads_create_ad_group",
    description=(
        "Create an ad group under a campaign. Always PAUSED. Set "
        "cpc_bid_usd for MANUAL_CPC campaigns; ignored for automated "
        "bid strategies."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "campaign_resource": {"type": "string"},
            "cpc_bid_usd": {"type": "number"},
            "ad_group_type": {
                "type": "string",
                "enum": ["SEARCH_STANDARD", "DISPLAY_STANDARD"],
            },
        },
        "required": ["name", "campaign_resource"],
    },
    risk=RiskClass.NET_WRITE,
    handler=_create_ad_group,
)


async def _add_keywords(args: dict, _ctx: ToolContext) -> ToolOutcome:
    ad_group_resource = str(args.get("ad_group_resource") or "").strip()
    keywords = args.get("keywords")
    if not ad_group_resource or not isinstance(keywords, list) or not keywords:
        return ToolOutcome(
            content="google_ads_add_keywords requires 'ad_group_resource' "
                    "and a non-empty 'keywords' list.",
            is_error=True,
        )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.add_keywords(ad_group_resource, keywords)
    except GoogleAdsError as e:
        return _surface(e)
    return ToolOutcome(
        content=f"Added {len(keywords)} keyword(s) to {ad_group_resource}.",
        data={"raw": payload},
    )


google_ads_add_keywords_tool = Tool(
    name="google_ads_add_keywords",
    description=(
        "Add keywords to an ad group. Each keyword is "
        "{text, match_type} where match_type is EXACT | PHRASE | "
        "BROAD. Google Ads charges per click on impressions that "
        "match — start narrow (EXACT + PHRASE) and broaden only if "
        "volume is thin."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "ad_group_resource": {"type": "string"},
            "keywords": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "match_type": {
                            "type": "string",
                            "enum": ["EXACT", "PHRASE", "BROAD"],
                        },
                    },
                    "required": ["text"],
                },
            },
        },
        "required": ["ad_group_resource", "keywords"],
    },
    risk=RiskClass.NET_WRITE,
    handler=_add_keywords,
)


async def _add_negative_keywords(
    args: dict, _ctx: ToolContext
) -> ToolOutcome:
    campaign_resource = str(args.get("campaign_resource") or "").strip()
    keywords = args.get("keywords")
    if not campaign_resource or not isinstance(keywords, list) or not keywords:
        return ToolOutcome(
            content="google_ads_add_negative_keywords requires "
                    "'campaign_resource' and a non-empty 'keywords' list.",
            is_error=True,
        )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.add_negative_keywords(
            campaign_resource, keywords
        )
    except GoogleAdsError as e:
        return _surface(e)
    return ToolOutcome(
        content=(
            f"Added {len(keywords)} negative keyword(s) to "
            f"{campaign_resource}."
        ),
        data={"raw": payload},
    )


google_ads_add_negative_keywords_tool = Tool(
    name="google_ads_add_negative_keywords",
    description=(
        "Add campaign-level negative keywords to stop ads from serving "
        "on junk searches ('free', 'cheap', 'jobs', competitor names "
        "the operator doesn't want to bid on). Same shape as "
        "add_keywords."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "campaign_resource": {"type": "string"},
            "keywords": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "match_type": {
                            "type": "string",
                            "enum": ["EXACT", "PHRASE", "BROAD"],
                        },
                    },
                    "required": ["text"],
                },
            },
        },
        "required": ["campaign_resource", "keywords"],
    },
    risk=RiskClass.NET_WRITE,
    handler=_add_negative_keywords,
)


async def _create_responsive_search_ad(
    args: dict, _ctx: ToolContext
) -> ToolOutcome:
    ad_group_resource = str(args.get("ad_group_resource") or "").strip()
    headlines = args.get("headlines") or []
    descriptions = args.get("descriptions") or []
    final_urls = args.get("final_urls") or []
    if not ad_group_resource:
        return ToolOutcome(
            content="google_ads_create_responsive_search_ad requires "
                    "'ad_group_resource'.",
            is_error=True,
        )
    if not isinstance(headlines, list) or not isinstance(descriptions, list):
        return ToolOutcome(
            content="'headlines' and 'descriptions' must be string arrays.",
            is_error=True,
        )
    if not isinstance(final_urls, list) or not final_urls:
        return ToolOutcome(
            content="'final_urls' must be a non-empty array of URLs.",
            is_error=True,
        )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.create_responsive_search_ad(
            ad_group_resource=ad_group_resource,
            headlines=[str(h) for h in headlines],
            descriptions=[str(d) for d in descriptions],
            final_urls=[str(u) for u in final_urls],
            path1=args.get("path1"),
            path2=args.get("path2"),
        )
    except GoogleAdsError as e:
        return _surface(e)
    results = payload.get("results") or []
    resource = (results[0] or {}).get("resourceName") if results else None
    return ToolOutcome(
        content=(
            f"Created responsive search ad (resource={resource}) in "
            "PAUSED status."
        ),
        data={"resource_name": resource, "raw": payload},
    )


google_ads_create_responsive_search_ad_tool = Tool(
    name="google_ads_create_responsive_search_ad",
    description=(
        "Create a responsive search ad under an ad group. Google "
        "auto-combines the 3-15 headlines + 2-4 descriptions into the "
        "best-performing ad variants at serve time. Always PAUSED."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "ad_group_resource": {"type": "string"},
            "headlines": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 15,
            },
            "descriptions": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 4,
            },
            "final_urls": {
                "type": "array",
                "items": {"type": "string"},
            },
            "path1": {"type": "string"},
            "path2": {"type": "string"},
        },
        "required": [
            "ad_group_resource", "headlines", "descriptions", "final_urls",
        ],
    },
    risk=RiskClass.NET_WRITE,
    handler=_create_responsive_search_ad,
)


# ── Status + budget (FINANCIAL when the change means spending) ───


async def _set_status(args: dict, _ctx: ToolContext) -> ToolOutcome:
    campaign_resource = str(args.get("campaign_resource") or "").strip()
    status = str(args.get("status") or "").strip().upper()
    if not campaign_resource or status not in {"ENABLED", "PAUSED", "REMOVED"}:
        return ToolOutcome(
            content="google_ads_set_status requires 'campaign_resource' "
                    "and 'status' (ENABLED | PAUSED | REMOVED).",
            is_error=True,
        )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.set_campaign_status(campaign_resource, status)
    except GoogleAdsError as e:
        return _surface(e)
    return ToolOutcome(
        content=f"Set {campaign_resource} → {status}.",
        data={"resource_name": campaign_resource, "status": status, "raw": payload},
    )


google_ads_set_status_tool = Tool(
    name="google_ads_set_status",
    description=(
        "Change the run status of a campaign. ENABLED starts spending; "
        "PAUSED halts; REMOVED is effectively archive. FINANCIAL "
        "because ENABLED spends money — expect an approval prompt "
        "until the agent's autonomy profile is raised."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "campaign_resource": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["ENABLED", "PAUSED", "REMOVED"],
            },
        },
        "required": ["campaign_resource", "status"],
    },
    risk=RiskClass.FINANCIAL,
    handler=_set_status,
)


async def _update_budget(args: dict, _ctx: ToolContext) -> ToolOutcome:
    budget_resource = str(args.get("budget_resource") or "").strip()
    daily_usd = args.get("daily_usd")
    if not budget_resource or daily_usd is None:
        return ToolOutcome(
            content="google_ads_update_budget requires "
                    "'budget_resource' and 'daily_usd'.",
            is_error=True,
        )
    try:
        daily_f = float(daily_usd)
    except (TypeError, ValueError):
        return ToolOutcome(
            content="daily_usd must be a number.", is_error=True,
        )
    if daily_f <= 0:
        return ToolOutcome(
            content="daily_usd must be positive.", is_error=True,
        )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.update_campaign_budget(
            budget_resource, amount_micros=int(daily_f * 1_000_000),
        )
    except GoogleAdsError as e:
        return _surface(e)
    return ToolOutcome(
        content=f"Updated budget {budget_resource} → ${daily_f:.2f}/day.",
        data={"raw": payload},
    )


google_ads_update_budget_tool = Tool(
    name="google_ads_update_budget",
    description=(
        "Change a campaign budget's daily amount in USD. FINANCIAL — "
        "directly changes what the account will spend."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "budget_resource": {"type": "string"},
            "daily_usd": {"type": "number"},
        },
        "required": ["budget_resource", "daily_usd"],
    },
    risk=RiskClass.FINANCIAL,
    handler=_update_budget,
)


# ── Bundle ───────────────────────────────────────────────────────


GOOGLE_ADS_TOOLS: list[Tool] = [
    google_ads_list_campaigns_tool,
    google_ads_list_ad_groups_tool,
    google_ads_list_ads_tool,
    google_ads_get_metrics_tool,
    google_ads_run_gaql_tool,
    google_ads_create_budget_tool,
    google_ads_create_campaign_tool,
    google_ads_create_ad_group_tool,
    google_ads_add_keywords_tool,
    google_ads_add_negative_keywords_tool,
    google_ads_create_responsive_search_ad_tool,
    google_ads_set_status_tool,
    google_ads_update_budget_tool,
]


# ── Helpers ──────────────────────────────────────────────────────


def _get(d: dict[str, Any], path: str) -> Any:
    """Pluck a dotted path out of a GAQL row dict. Google returns
    nested JSON like {campaign: {id: '...', name: '...'}} — this is
    the terse equivalent of ``d['campaign']['id']`` that short-
    circuits to None on any missing link in the chain."""
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _flatten_campaign_row(r: dict[str, Any]) -> dict[str, Any]:
    budget_micros = _get(r, "campaignBudget.amountMicros")
    try:
        daily_budget = (
            float(budget_micros) / 1_000_000 if budget_micros else None
        )
    except (TypeError, ValueError):
        daily_budget = None
    return {
        "id": _get(r, "campaign.id"),
        "name": _get(r, "campaign.name"),
        "status": _get(r, "campaign.status"),
        "channel_type": _get(r, "campaign.advertisingChannelType"),
        "bidding_strategy_type": _get(r, "campaign.biddingStrategyType"),
        "daily_budget_usd": (
            f"{daily_budget:.2f}" if daily_budget is not None else "n/a"
        ),
    }
