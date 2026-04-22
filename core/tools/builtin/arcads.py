"""Tools that drive the Arcads external API.

Three tools that together cover the end-to-end flow the
`ugc_video_agent` needs:

  arcads_list_products      pick which product context the render
                            should run against (every generate call
                            needs a productId).
  arcads_video_generate     fire a seedance-2.0 (default) UGC video
                            generation from a prompt. Returns the
                            asset id the agent then polls.
  arcads_video_status       poll the asset endpoint until it's
                            `generated` or `failed`; tools stay
                            synchronous so the orchestrator's loop
                            owns the polling rhythm.

All three resolve their API key through the shared secret store so a
runtime secret update via Settings → API Keys takes effect without a
daemon restart.
"""

from __future__ import annotations

from typing import Any

from core.integrations.arcads import ArcadsClient, ArcadsError
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.secrets import resolve_secret
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.tools.arcads")


def _client_or_error() -> ArcadsClient | ToolOutcome:
    api_key = resolve_secret("arcads_api_key", None)
    if not api_key:
        return ToolOutcome(
            content=(
                "Arcads API key is not configured. Add it at "
                "Settings → API Keys (Creative AI → "
                "arcads_api_key) or set the ARCADS_API_KEY env var."
            ),
            is_error=True,
        )
    return ArcadsClient(api_key=api_key)


# ── arcads_list_products ────────────────────────────────────────────


async def _list_products_handler(
    _args: dict[str, Any], _ctx: ToolContext,
) -> ToolOutcome:
    client = _client_or_error()
    if isinstance(client, ToolOutcome):
        return client
    try:
        raw = await client.list_products()
    except ArcadsError as e:
        return ToolOutcome(content=str(e), is_error=True)
    products: list[dict[str, Any]] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        products.append(
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "description": p.get("description"),
                "targetAudience": p.get("targetAudience"),
            }
        )
    if not products:
        return ToolOutcome(
            content=(
                "No products found on this Arcads workspace. Create "
                "one from the Arcads dashboard first."
            ),
            data={"products": []},
        )
    lines = [
        f"- {p['name'] or '(no name)'} (id={p['id']})"
        for p in products
    ]
    return ToolOutcome(
        content=(
            f"{len(products)} Arcads product"
            f"{'s' if len(products) != 1 else ''}:\n" + "\n".join(lines)
        ),
        data={"products": products, "count": len(products)},
    )


arcads_list_products_tool = Tool(
    name="arcads_list_products",
    description=(
        "List the Arcads products on the current workspace. Every "
        "Arcads video / image generation call needs a productId; use "
        "this to pick one (or to confirm the operator's default). "
        "Returns id + name + description per product. Read-only."
    ),
    input_schema={"type": "object", "properties": {}},
    risk=RiskClass.NET_READ,
    handler=_list_products_handler,
)


# ── arcads_video_generate ───────────────────────────────────────────


async def _video_generate_handler(
    args: dict[str, Any], _ctx: ToolContext,
) -> ToolOutcome:
    product_id = str(args.get("product_id") or "").strip()
    prompt = str(args.get("prompt") or "").strip()
    if not product_id:
        return ToolOutcome(
            content="arcads_video_generate requires 'product_id'.",
            is_error=True,
        )
    if not prompt:
        return ToolOutcome(
            content="arcads_video_generate requires a non-empty 'prompt'.",
            is_error=True,
        )
    client = _client_or_error()
    if isinstance(client, ToolOutcome):
        return client
    kwargs: dict[str, Any] = {
        "product_id": product_id,
        "prompt": prompt,
    }
    if args.get("model"):
        kwargs["model"] = str(args["model"])
    if args.get("aspect_ratio"):
        kwargs["aspect_ratio"] = str(args["aspect_ratio"])
    if args.get("duration_s") is not None:
        try:
            kwargs["duration_s"] = int(args["duration_s"])
        except (TypeError, ValueError):
            return ToolOutcome(
                content="'duration_s' must be an integer.",
                is_error=True,
            )
    if args.get("resolution"):
        kwargs["resolution"] = str(args["resolution"])
    if args.get("audio_enabled") is not None:
        kwargs["audio_enabled"] = bool(args["audio_enabled"])
    if isinstance(args.get("reference_images"), list):
        kwargs["reference_images"] = [str(x) for x in args["reference_images"]]
    try:
        asset = await client.create_video(**kwargs)
    except ArcadsError as e:
        return ToolOutcome(content=str(e), is_error=True)
    asset_id = asset.get("id") or ""
    status = asset.get("status") or "unknown"
    credits = (asset.get("data") or {}).get("creditsCharged")
    return ToolOutcome(
        content=(
            f"Arcads render started: asset {asset_id} (status: {status})"
            + (f", ~{credits} credits charged." if credits is not None else ".")
            + " Poll arcads_video_status until status=generated."
        ),
        data={
            "asset_id": asset_id,
            "status": status,
            "credits_charged": credits,
            "asset": asset,
        },
    )


arcads_video_generate_tool = Tool(
    name="arcads_video_generate",
    description=(
        "Start an Arcads video generation. Returns an asset id to "
        "poll with arcads_video_status. Default model is "
        "'seedance-2.0' (UGC selfie-style, supports audioEnabled + "
        "reference images). Pass model='sora2' / 'veo31' / "
        "'kling-3.0' / 'grok-video' for other engines. "
        "Credits charge at create time even if content check "
        "later fails, so sanity-check the prompt first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "product_id": {
                "type": "string",
                "description": (
                    "Arcads productId the render belongs to. Use "
                    "arcads_list_products to discover this."
                ),
            },
            "prompt": {
                "type": "string",
                "description": (
                    "The generation prompt. Follow the model-"
                    "specific prompting guide — Seedance 2.0 UGC "
                    "works best with a 9-layer formula (see Arcads "
                    "skill docs)."
                ),
            },
            "model": {
                "type": "string",
                "enum": [
                    "seedance-2.0",
                    "sora2",
                    "sora2-pro",
                    "veo31",
                    "kling-2.6",
                    "kling-3.0",
                    "grok-video",
                ],
                "description": "Defaults to seedance-2.0.",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["9:16", "16:9", "1:1"],
                "description": (
                    "Defaults to 9:16 (Reels / Shorts / TikTok)."
                ),
            },
            "duration_s": {
                "type": "integer",
                "description": (
                    "Clip duration in seconds. Per-model range "
                    "applies (Seedance 2.0: 4-15; Kling 3.0: 3-15; "
                    "Sora 2: 4/8/12/16/20). Omit on Veo 3.1 — it "
                    "auto-sizes."
                ),
            },
            "resolution": {
                "type": "string",
                "description": (
                    "Optional resolution hint. Seedance 2.0 accepts "
                    "480p / 720p; Sora 2 accepts 720p / 1080p."
                ),
            },
            "audio_enabled": {
                "type": "boolean",
                "description": (
                    "Seedance 2.0 only — enables the lip-synced "
                    "audio track from the prompt."
                ),
            },
            "reference_images": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Array of presigned filePath strings from "
                    "POST /v1/file-upload/get-presigned-url. For "
                    "image-to-video on seedance-2.0 or "
                    "startFrame-equivalents on other models."
                ),
            },
        },
        "required": ["product_id", "prompt"],
    },
    risk=RiskClass.NET_WRITE,
    handler=_video_generate_handler,
)


# ── arcads_video_status ─────────────────────────────────────────────


async def _video_status_handler(
    args: dict[str, Any], _ctx: ToolContext,
) -> ToolOutcome:
    asset_id = str(args.get("asset_id") or "").strip()
    if not asset_id:
        return ToolOutcome(
            content="arcads_video_status requires 'asset_id'.",
            is_error=True,
        )
    client = _client_or_error()
    if isinstance(client, ToolOutcome):
        return client
    try:
        asset = await client.get_asset(asset_id)
    except ArcadsError as e:
        return ToolOutcome(content=str(e), is_error=True)
    status = asset.get("status") or "unknown"
    # Arcads stores the final URL on `url` for most asset types and on
    # `data.videoUrl` for video-specific responses. Expose both so the
    # planner can grab whichever is present.
    url = asset.get("url")
    data = asset.get("data") or {}
    video_url = data.get("videoUrl") or url
    msg_bits = [f"Arcads asset {asset_id}: status={status}"]
    if status == "generated" and video_url:
        msg_bits.append(f"url={video_url}")
    elif status == "failed":
        err = data.get("error") or {}
        if err.get("message"):
            msg_bits.append(f"error={err['message']}")
    return ToolOutcome(
        content=". ".join(msg_bits) + ".",
        data={
            "asset_id": asset_id,
            "status": status,
            "video_url": video_url,
            "asset": asset,
        },
    )


arcads_video_status_tool = Tool(
    name="arcads_video_status",
    description=(
        "Check the status of a running Arcads render. Returns the "
        "status enum (created | pending | generated | failed | "
        "uploaded) plus the video URL once status == generated. "
        "Planner polls this every ~20 seconds until the render "
        "completes. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "asset_id": {
                "type": "string",
                "description": (
                    "Asset id returned from arcads_video_generate."
                ),
            },
        },
        "required": ["asset_id"],
    },
    risk=RiskClass.NET_READ,
    handler=_video_status_handler,
)


ARCADS_TOOLS = (
    arcads_list_products_tool,
    arcads_video_generate_tool,
    arcads_video_status_tool,
)


__all__ = [
    "ARCADS_TOOLS",
    "arcads_list_products_tool",
    "arcads_video_generate_tool",
    "arcads_video_status_tool",
]
