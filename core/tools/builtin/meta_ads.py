"""Meta Marketing tools for the meta_ads_agent — full operator, not a
reporter.

Twelve tools cover the campaign → ad set → ad → creative flow plus
insights. Risk classes are assigned per-tool based on real-world
impact:

    READ           list_campaigns / list_adsets / list_ads / get_insights
    NET_WRITE      creates in PAUSED, status to PAUSED|ARCHIVED, upload
    FINANCIAL      status to ACTIVE (spending money starts here) and
                   budget updates on any object

Every handler reads credentials via ``resolve_secret`` so the operator
pastes them in Settings → API Keys rather than editing env vars.
Missing config returns a clean ``is_error`` outcome telling the agent
which keys to set; the agent surfaces that to the user instead of
crashing the plan.
"""

from __future__ import annotations

from core.config import get_settings
from core.integrations.meta_ads import (
    MetaAdsClient,
    MetaAdsConfig,
    MetaAdsError,
)
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.secrets import resolve_secret
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.tools.meta_ads")


def _client() -> MetaAdsClient | tuple[None, str]:
    """Build a MetaAdsClient from the resolved secrets. Returns the
    client OR a tuple of (None, reason) so callers can emit a clean
    is_error ToolOutcome."""
    s = get_settings()
    token = resolve_secret("meta_access_token", s.meta_access_token)
    account = resolve_secret("meta_ad_account_id", s.meta_ad_account_id)
    if not token or not account:
        return (
            None,
            "Meta Ads not configured. Add meta_access_token + "
            "meta_ad_account_id in Settings → API Keys.",
        )
    page_id = resolve_secret("meta_page_id", s.meta_page_id)
    return MetaAdsClient(
        MetaAdsConfig(
            access_token=token,
            ad_account_id=account,
            page_id=page_id,
        )
    )


def _unwrap(client_or_err):
    """Flatten _client() into (client, error_outcome)."""
    if isinstance(client_or_err, tuple):
        return None, ToolOutcome(content=client_or_err[1], is_error=True)
    return client_or_err, None


def _surface(e: MetaAdsError) -> ToolOutcome:
    return ToolOutcome(
        content=f"Meta Ads API {e.status}: {e.message}",
        is_error=True,
        data={"status": e.status, "raw": e.raw},
    )


# ── Reads ────────────────────────────────────────────────────────


async def _list_campaigns(args: dict, _ctx: ToolContext) -> ToolOutcome:
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        rows = await client.list_campaigns(
            status=args.get("status"),
            limit=int(args.get("limit") or 50),
        )
    except MetaAdsError as e:
        return _surface(e)
    lines = [
        f"- {r['name']} (id={r['id']}, {r.get('effective_status')}, "
        f"obj={r.get('objective')})"
        for r in rows
    ] or ["(no campaigns)"]
    return ToolOutcome(
        content=f"{len(rows)} campaign(s):\n" + "\n".join(lines),
        data={"campaigns": rows},
    )


meta_ads_list_campaigns_tool = Tool(
    name="meta_ads_list_campaigns",
    description=(
        "List campaigns on the operator's Meta ad account. Optional "
        "`status` filters to a single effective_status (e.g. ACTIVE, "
        "PAUSED, DELETED). Returns id, name, objective, status, "
        "budget."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Filter to effective_status. Optional.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
    },
    risk=RiskClass.NET_READ,
    handler=_list_campaigns,
)


async def _list_adsets(args: dict, _ctx: ToolContext) -> ToolOutcome:
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        rows = await client.list_adsets(
            campaign_id=args.get("campaign_id"),
            limit=int(args.get("limit") or 50),
        )
    except MetaAdsError as e:
        return _surface(e)
    lines = [
        f"- {r['name']} (id={r['id']}, {r.get('effective_status')}, "
        f"opt={r.get('optimization_goal')})"
        for r in rows
    ] or ["(no ad sets)"]
    return ToolOutcome(
        content=f"{len(rows)} ad set(s):\n" + "\n".join(lines),
        data={"adsets": rows},
    )


meta_ads_list_adsets_tool = Tool(
    name="meta_ads_list_adsets",
    description=(
        "List ad sets. Pass `campaign_id` to scope to one campaign; "
        "omit for every ad set on the account."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "campaign_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
    },
    risk=RiskClass.NET_READ,
    handler=_list_adsets,
)


async def _list_ads(args: dict, _ctx: ToolContext) -> ToolOutcome:
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        rows = await client.list_ads(
            adset_id=args.get("adset_id"),
            limit=int(args.get("limit") or 50),
        )
    except MetaAdsError as e:
        return _surface(e)
    lines = [
        f"- {r['name']} (id={r['id']}, {r.get('effective_status')})"
        for r in rows
    ] or ["(no ads)"]
    return ToolOutcome(
        content=f"{len(rows)} ad(s):\n" + "\n".join(lines),
        data={"ads": rows},
    )


meta_ads_list_ads_tool = Tool(
    name="meta_ads_list_ads",
    description=(
        "List ads. Pass `adset_id` to scope to one ad set; omit for "
        "every ad on the account."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "adset_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
    },
    risk=RiskClass.NET_READ,
    handler=_list_ads,
)


async def _get_insights(args: dict, _ctx: ToolContext) -> ToolOutcome:
    object_id = str(args.get("object_id") or "").strip()
    if not object_id:
        return ToolOutcome(
            content="meta_ads_get_insights requires 'object_id' "
                    "(campaign_id / adset_id / ad_id).",
            is_error=True,
        )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        rows = await client.get_insights(
            object_id,
            level=str(args.get("level") or "ad"),
            date_preset=str(args.get("date_preset") or "last_7d"),
            fields=args.get("fields"),
        )
    except MetaAdsError as e:
        return _surface(e)
    if not rows:
        return ToolOutcome(
            content="(no insights in range — object may be too new "
                    "or have no spend)",
            data={"insights": []},
        )
    top = rows[0]
    summary_bits = [
        f"{k}={top.get(k)}"
        for k in ("impressions", "clicks", "spend", "ctr", "cpc", "cpm")
        if k in top
    ]
    return ToolOutcome(
        content=(
            f"Insights ({len(rows)} row[s]): "
            + " · ".join(summary_bits)
        ),
        data={"insights": rows},
    )


meta_ads_get_insights_tool = Tool(
    name="meta_ads_get_insights",
    description=(
        "Fetch performance insights for a campaign / ad set / ad. "
        "Defaults to ad-level over last 7 days. Useful fields: "
        "impressions, clicks, spend, ctr, cpc, cpm, reach, frequency, "
        "conversions, actions, cost_per_action_type."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "object_id": {"type": "string"},
            "level": {
                "type": "string",
                "enum": ["account", "campaign", "adset", "ad"],
            },
            "date_preset": {
                "type": "string",
                "description": (
                    "Meta preset like last_7d, last_14d, last_30d, "
                    "this_month, yesterday."
                ),
            },
            "fields": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["object_id"],
    },
    risk=RiskClass.NET_READ,
    handler=_get_insights,
)


# ── Creates (NET_WRITE; always PAUSED) ───────────────────────────


async def _create_campaign(args: dict, _ctx: ToolContext) -> ToolOutcome:
    name = str(args.get("name") or "").strip()
    objective = str(args.get("objective") or "").strip()
    if not name or not objective:
        return ToolOutcome(
            content="meta_ads_create_campaign requires 'name' and "
                    "'objective' (e.g. OUTCOME_TRAFFIC, OUTCOME_SALES).",
            is_error=True,
        )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.create_campaign(
            name=name,
            objective=objective,
            special_ad_categories=args.get("special_ad_categories") or [],
            daily_budget=args.get("daily_budget"),
            lifetime_budget=args.get("lifetime_budget"),
        )
    except MetaAdsError as e:
        return _surface(e)
    return ToolOutcome(
        content=(
            f"Created campaign '{name}' (id={payload.get('id')}) in PAUSED "
            "status. Activate with meta_ads_set_status when ready."
        ),
        data=payload,
    )


meta_ads_create_campaign_tool = Tool(
    name="meta_ads_create_campaign",
    description=(
        "Create a new campaign. Always starts PAUSED — the operator "
        "or a subsequent meta_ads_set_status call with status=ACTIVE "
        "is required before spending starts. Objectives: "
        "OUTCOME_TRAFFIC, OUTCOME_ENGAGEMENT, OUTCOME_LEADS, "
        "OUTCOME_SALES, OUTCOME_AWARENESS, OUTCOME_APP_PROMOTION."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "objective": {"type": "string"},
            "daily_budget": {
                "type": "integer",
                "description": "Cents (USD cents for USD accounts).",
            },
            "lifetime_budget": {"type": "integer"},
            "special_ad_categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Required for housing / employment / credit / "
                    "social-issues ads. Empty array for most."
                ),
            },
        },
        "required": ["name", "objective"],
    },
    risk=RiskClass.NET_WRITE,
    handler=_create_campaign,
)


async def _create_adset(args: dict, _ctx: ToolContext) -> ToolOutcome:
    required = ("campaign_id", "name", "optimization_goal", "billing_event")
    for k in required:
        if not str(args.get(k) or "").strip():
            return ToolOutcome(
                content=f"meta_ads_create_adset requires '{k}'.",
                is_error=True,
            )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.create_adset(
            campaign_id=args["campaign_id"],
            name=args["name"],
            optimization_goal=args["optimization_goal"],
            billing_event=args["billing_event"],
            daily_budget=args.get("daily_budget"),
            lifetime_budget=args.get("lifetime_budget"),
            bid_amount=args.get("bid_amount"),
            targeting=args.get("targeting"),
            start_time=args.get("start_time"),
            end_time=args.get("end_time"),
        )
    except MetaAdsError as e:
        return _surface(e)
    return ToolOutcome(
        content=(
            f"Created ad set '{args['name']}' (id={payload.get('id')}) "
            "in PAUSED status."
        ),
        data=payload,
    )


meta_ads_create_adset_tool = Tool(
    name="meta_ads_create_adset",
    description=(
        "Create an ad set under a campaign. Always PAUSED. "
        "optimization_goal: OFFSITE_CONVERSIONS, LINK_CLICKS, "
        "REACH, IMPRESSIONS, LEAD_GENERATION, THRUPLAY, etc. "
        "billing_event: IMPRESSIONS, LINK_CLICKS, THRUPLAY, etc. "
        "targeting is a Meta spec dict; defaults to {geo_locations: "
        "{countries: ['US']}} if omitted."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "campaign_id": {"type": "string"},
            "name": {"type": "string"},
            "optimization_goal": {"type": "string"},
            "billing_event": {"type": "string"},
            "daily_budget": {"type": "integer"},
            "lifetime_budget": {"type": "integer"},
            "bid_amount": {"type": "integer"},
            "targeting": {"type": "object"},
            "start_time": {"type": "string"},
            "end_time": {"type": "string"},
        },
        "required": [
            "campaign_id", "name", "optimization_goal", "billing_event",
        ],
    },
    risk=RiskClass.NET_WRITE,
    handler=_create_adset,
)


async def _upload_image(args: dict, ctx: ToolContext) -> ToolOutcome:
    rel = str(args.get("path") or "").strip()
    if not rel:
        return ToolOutcome(
            content="meta_ads_upload_image requires 'path' (workspace-"
                    "relative, e.g. 'creative/1713-abc.png').",
            is_error=True,
        )
    root = (
        ctx.sandbox_root.expanduser().resolve()
        if ctx.sandbox_root is not None
        else get_settings().workspace_dir.expanduser().resolve()
    )
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return ToolOutcome(
            content=f"path escapes workspace: {rel}",
            is_error=True,
        )
    if not candidate.exists() or not candidate.is_file():
        return ToolOutcome(content=f"not found: {rel}", is_error=True)
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.upload_image(candidate)
    except MetaAdsError as e:
        return _surface(e)
    return ToolOutcome(
        content=(
            f"Uploaded {rel}. hash={payload.get('hash')} — use this "
            "as image_hash on meta_ads_create_creative."
        ),
        data=payload,
    )


meta_ads_upload_image_tool = Tool(
    name="meta_ads_upload_image",
    description=(
        "Upload an image from the workspace to the Meta ad account's "
        "image library. Returns `hash` you pass to "
        "meta_ads_create_creative as image_hash. Typical flow: "
        "creative_content_agent renders an image to workspace/"
        "creative/X.png → meta_ads_upload_image → meta_ads_create_"
        "creative → meta_ads_create_ad."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path to a PNG/JPG.",
            }
        },
        "required": ["path"],
    },
    risk=RiskClass.NET_WRITE,
    handler=_upload_image,
)


async def _upload_video(args: dict, ctx: ToolContext) -> ToolOutcome:
    rel = str(args.get("path") or "").strip()
    if not rel:
        return ToolOutcome(
            content="meta_ads_upload_video requires 'path' (workspace-"
                    "relative MP4).",
            is_error=True,
        )
    root = (
        ctx.sandbox_root.expanduser().resolve()
        if ctx.sandbox_root is not None
        else get_settings().workspace_dir.expanduser().resolve()
    )
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return ToolOutcome(
            content=f"path escapes workspace: {rel}",
            is_error=True,
        )
    if not candidate.exists() or not candidate.is_file():
        return ToolOutcome(content=f"not found: {rel}", is_error=True)
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.upload_video(
            candidate, title=args.get("title")
        )
    except MetaAdsError as e:
        return _surface(e)
    return ToolOutcome(
        content=(
            f"Uploaded {rel}. video_id={payload.get('id')} — use this "
            "as video_id on meta_ads_create_creative."
        ),
        data=payload,
    )


meta_ads_upload_video_tool = Tool(
    name="meta_ads_upload_video",
    description=(
        "Upload a video from the workspace (e.g. creative_content_"
        "agent output) to the Meta ad account. Returns `id` for use "
        "as video_id on meta_ads_create_creative."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "title": {"type": "string"},
        },
        "required": ["path"],
    },
    risk=RiskClass.NET_WRITE,
    handler=_upload_video,
)


async def _create_creative(args: dict, _ctx: ToolContext) -> ToolOutcome:
    name = str(args.get("name") or "").strip()
    if not name:
        return ToolOutcome(
            content="meta_ads_create_creative requires 'name'.",
            is_error=True,
        )
    if not args.get("image_hash") and not args.get("video_id"):
        return ToolOutcome(
            content=(
                "meta_ads_create_creative requires either "
                "'image_hash' (from meta_ads_upload_image) or "
                "'video_id' (from meta_ads_upload_video)."
            ),
            is_error=True,
        )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.create_creative(
            name=name,
            page_id=args.get("page_id"),
            image_hash=args.get("image_hash"),
            video_id=args.get("video_id"),
            message=args.get("message"),
            link=args.get("link"),
            headline=args.get("headline"),
            description=args.get("description"),
            call_to_action_type=args.get("call_to_action_type"),
        )
    except MetaAdsError as e:
        return _surface(e)
    return ToolOutcome(
        content=(
            f"Created ad creative '{name}' (id={payload.get('id')}). "
            "Feed this id into meta_ads_create_ad as creative_id."
        ),
        data=payload,
    )


meta_ads_create_creative_tool = Tool(
    name="meta_ads_create_creative",
    description=(
        "Create an ad creative. Provide either image_hash OR video_id "
        "(from the upload tools). Typical CTAs: LEARN_MORE, SHOP_NOW, "
        "SIGN_UP, DOWNLOAD, CONTACT_US, GET_QUOTE, BOOK_TRAVEL. "
        "Uses meta_page_id from Settings as the owning page unless "
        "you pass a page_id override."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "page_id": {"type": "string"},
            "image_hash": {"type": "string"},
            "video_id": {"type": "string"},
            "message": {
                "type": "string",
                "description": "Primary text / caption body.",
            },
            "link": {"type": "string"},
            "headline": {"type": "string"},
            "description": {"type": "string"},
            "call_to_action_type": {"type": "string"},
        },
        "required": ["name"],
    },
    risk=RiskClass.NET_WRITE,
    handler=_create_creative,
)


async def _create_ad(args: dict, _ctx: ToolContext) -> ToolOutcome:
    for k in ("adset_id", "name", "creative_id"):
        if not str(args.get(k) or "").strip():
            return ToolOutcome(
                content=f"meta_ads_create_ad requires '{k}'.",
                is_error=True,
            )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.create_ad(
            adset_id=args["adset_id"],
            name=args["name"],
            creative_id=args["creative_id"],
        )
    except MetaAdsError as e:
        return _surface(e)
    return ToolOutcome(
        content=(
            f"Created ad '{args['name']}' (id={payload.get('id')}) "
            "in PAUSED status. Activate with meta_ads_set_status "
            "when ready."
        ),
        data=payload,
    )


meta_ads_create_ad_tool = Tool(
    name="meta_ads_create_ad",
    description=(
        "Create an ad under an ad set, tying it to an existing creative. "
        "Always PAUSED. After creation, call meta_ads_set_status with "
        "status=ACTIVE to start serving (requires approval — costs "
        "money)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "adset_id": {"type": "string"},
            "name": {"type": "string"},
            "creative_id": {"type": "string"},
        },
        "required": ["adset_id", "name", "creative_id"],
    },
    risk=RiskClass.NET_WRITE,
    handler=_create_ad,
)


# ── Status + budget (FINANCIAL when the change means spending) ───


async def _set_status(args: dict, _ctx: ToolContext) -> ToolOutcome:
    object_id = str(args.get("object_id") or "").strip()
    status = str(args.get("status") or "").strip().upper()
    if not object_id or not status:
        return ToolOutcome(
            content="meta_ads_set_status requires 'object_id' and "
                    "'status' (ACTIVE|PAUSED|ARCHIVED|DELETED).",
            is_error=True,
        )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.set_status(object_id, status)
    except MetaAdsError as e:
        return _surface(e)
    return ToolOutcome(
        content=f"Set {object_id} to {status}.",
        data={**payload, "object_id": object_id, "status": status},
    )


meta_ads_set_status_tool = Tool(
    name="meta_ads_set_status",
    description=(
        "Change the run status of a campaign, ad set, or ad. ACTIVE "
        "means Meta starts spending; PAUSED halts; ARCHIVED removes "
        "from the main view; DELETED is permanent. This tool is "
        "flagged FINANCIAL because activation spends money — expect "
        "an approval prompt until the agent's autonomy profile is "
        "raised."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "object_id": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["ACTIVE", "PAUSED", "ARCHIVED", "DELETED"],
            },
        },
        "required": ["object_id", "status"],
    },
    risk=RiskClass.FINANCIAL,
    handler=_set_status,
)


async def _update_budget(args: dict, _ctx: ToolContext) -> ToolOutcome:
    object_id = str(args.get("object_id") or "").strip()
    if not object_id:
        return ToolOutcome(
            content="meta_ads_update_budget requires 'object_id'.",
            is_error=True,
        )
    if (
        args.get("daily_budget") is None
        and args.get("lifetime_budget") is None
    ):
        return ToolOutcome(
            content="pass daily_budget or lifetime_budget (cents).",
            is_error=True,
        )
    client, err = _unwrap(_client())
    if err:
        return err
    try:
        payload = await client.update_budget(
            object_id,
            daily_budget=args.get("daily_budget"),
            lifetime_budget=args.get("lifetime_budget"),
        )
    except MetaAdsError as e:
        return _surface(e)
    return ToolOutcome(
        content=f"Updated budget on {object_id}.",
        data=payload,
    )


meta_ads_update_budget_tool = Tool(
    name="meta_ads_update_budget",
    description=(
        "Change daily_budget or lifetime_budget on a campaign / ad "
        "set. FINANCIAL — changes what the account spends."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "object_id": {"type": "string"},
            "daily_budget": {
                "type": "integer",
                "description": "Cents (USD cents for USD accounts).",
            },
            "lifetime_budget": {"type": "integer"},
        },
        "required": ["object_id"],
    },
    risk=RiskClass.FINANCIAL,
    handler=_update_budget,
)


META_ADS_TOOLS: list[Tool] = [
    meta_ads_list_campaigns_tool,
    meta_ads_list_adsets_tool,
    meta_ads_list_ads_tool,
    meta_ads_get_insights_tool,
    meta_ads_create_campaign_tool,
    meta_ads_create_adset_tool,
    meta_ads_upload_image_tool,
    meta_ads_upload_video_tool,
    meta_ads_create_creative_tool,
    meta_ads_create_ad_tool,
    meta_ads_set_status_tool,
    meta_ads_update_budget_tool,
]
