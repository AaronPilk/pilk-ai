"""UGC scout tools — creator discovery + email enrichment + CSV export.

Six tools the ugc_scout_agent drives:

    ugc_instagram_hashtag_search   NET_READ   hashtag → posts (via Apify)
    ugc_instagram_profile          NET_READ   username → profile + posts
    ugc_tiktok_hashtag_search      NET_READ   hashtag → videos (via Apify)
    ugc_tiktok_profile             NET_READ   username → profile + videos
    ugc_find_email                 NET_READ   bio regex + Hunter.io fallback
    ugc_export_csv                 WRITE_LOCAL workspace/ugc/<brief>.csv

The scoring / rubric live in the agent's system prompt — we deliberately
keep these tools as pure data-fetch + export surfaces, no LLM inside a
tool. That keeps the cost accounting clean (each LLM analysis call
shows up on the agent's ledger, not hidden inside a tool) and makes
the tools trivially testable with fake API responses.

Creator data on the way out of a search tool is normalised into a
small dict with the subset of fields that actually matter to the
agent: handle, platform, followers, bio, email_hint, recent_post_urls,
raw. The raw payload is preserved under ``raw`` so the agent can reach
further into the upstream JSON when it needs to.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from core.config import get_settings
from core.integrations.apify import ApifyClient, ApifyConfig, ApifyError
from core.integrations.hunter import HunterClient, HunterConfig, HunterError
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.secrets import resolve_secret
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.tools.ugc")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# ── Client builders ──────────────────────────────────────────────


def _apify() -> tuple[ApifyClient | None, str | None]:
    s = get_settings()
    token = resolve_secret("apify_api_token", s.apify_api_token)
    if not token:
        return (
            None,
            "Apify not configured. Add apify_api_token in Settings → "
            "API Keys — the UGC scout uses Apify actors for IG / TikTok "
            "discovery.",
        )
    return ApifyClient(ApifyConfig(api_token=token)), None


def _hunter() -> tuple[HunterClient | None, str | None]:
    s = get_settings()
    key = resolve_secret("hunter_io_api_key", s.hunter_io_api_key)
    if not key:
        return (
            None,
            "Hunter.io not configured. Add hunter_io_api_key in "
            "Settings → API Keys for email enrichment beyond bio scrape.",
        )
    return HunterClient(HunterConfig(api_key=key)), None


def _surface_apify(e: ApifyError) -> ToolOutcome:
    return ToolOutcome(
        content=f"Apify {e.status}: {e.message}",
        is_error=True,
        data={"status": e.status, "raw": e.raw},
    )


def _surface_hunter(e: HunterError) -> ToolOutcome:
    return ToolOutcome(
        content=f"Hunter.io {e.status}: {e.message}",
        is_error=True,
        data={"status": e.status, "raw": e.raw},
    )


# ── Normalisation helpers ────────────────────────────────────────


def _normalise_ig_post(item: dict[str, Any]) -> dict[str, Any]:
    """Project an apify/instagram-hashtag-scraper item down to the
    fields the agent reasons over. Preserve the full raw under ``raw``."""
    owner = item.get("ownerUsername") or item.get("owner", {}).get("username")
    return {
        "handle": owner,
        "platform": "instagram",
        "caption": item.get("caption") or "",
        "post_url": item.get("url") or item.get("shortcodeUrl"),
        "media_type": item.get("type") or item.get("mediaType"),
        "likes": item.get("likesCount") or item.get("likes"),
        "comments": item.get("commentsCount") or item.get("comments"),
        "display_url": item.get("displayUrl") or item.get("imageUrl"),
        "video_url": item.get("videoUrl"),
        "taken_at": item.get("timestamp") or item.get("taken_at"),
        "raw": item,
    }


def _normalise_ig_profile(item: dict[str, Any]) -> dict[str, Any]:
    """apify/instagram-scraper detail payload → normalised creator."""
    bio = item.get("biography") or ""
    posts = item.get("latestPosts") or item.get("posts") or []
    recent = [
        {
            "post_url": p.get("url"),
            "caption": p.get("caption"),
            "likes": p.get("likesCount"),
            "comments": p.get("commentsCount"),
            "type": p.get("type"),
            "video_url": p.get("videoUrl"),
            "display_url": p.get("displayUrl"),
        }
        for p in posts
    ]
    return {
        "handle": item.get("username"),
        "platform": "instagram",
        "full_name": item.get("fullName"),
        "followers": item.get("followersCount"),
        "following": item.get("followsCount"),
        "posts_count": item.get("postsCount"),
        "bio": bio,
        "bio_email": _extract_bio_email(bio),
        "external_url": item.get("externalUrl"),
        "is_business": item.get("isBusinessAccount"),
        "recent_posts": recent,
        "raw": item,
    }


def _normalise_tiktok_item(item: dict[str, Any]) -> dict[str, Any]:
    """clockworks/tiktok-scraper item → normalised creator + video row."""
    author = item.get("authorMeta") or {}
    bio = author.get("signature") or ""
    return {
        "handle": author.get("name") or author.get("nickName"),
        "platform": "tiktok",
        "display_name": author.get("nickName"),
        "followers": author.get("fans") or author.get("followerCount"),
        "following": author.get("following") or author.get("followingCount"),
        "videos_count": author.get("video") or author.get("videoCount"),
        "bio": bio,
        "bio_email": _extract_bio_email(bio),
        "verified": author.get("verified"),
        "post": {
            "post_url": item.get("webVideoUrl") or item.get("videoUrl"),
            "caption": item.get("text"),
            "plays": item.get("playCount"),
            "likes": item.get("diggCount"),
            "shares": item.get("shareCount"),
            "comments": item.get("commentCount"),
            "cover_url": item.get("covers", {}).get("default")
                        if isinstance(item.get("covers"), dict)
                        else None,
        },
        "raw": item,
    }


def _extract_bio_email(bio: str) -> str | None:
    """Pull the first email out of a bio string, if any. Lots of
    creators put it in their IG/TikTok bio in plain text."""
    if not bio:
        return None
    m = EMAIL_RE.search(bio)
    return m.group(0) if m else None


def _uniq_by_handle(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse multiple posts-by-same-handle into one row per creator.
    Keeps the first row's fields; appends additional post_urls under
    ``other_posts`` so downstream dedupe still sees the multi-hit."""
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        h = (r.get("handle") or "").lower()
        if not h:
            continue
        if h not in seen:
            seen[h] = {**r, "other_posts": []}
        else:
            url = r.get("post_url")
            if url and url != seen[h].get("post_url"):
                seen[h]["other_posts"].append(url)
    return list(seen.values())


# ── Search tools ─────────────────────────────────────────────────


async def _ig_hashtag_search(args: dict, _ctx: ToolContext) -> ToolOutcome:
    hashtag = str(args.get("hashtag") or "").strip()
    if not hashtag:
        return ToolOutcome(
            content="ugc_instagram_hashtag_search requires 'hashtag'.",
            is_error=True,
        )
    client, err = _apify()
    if err:
        return ToolOutcome(content=err, is_error=True)
    try:
        items = await client.instagram_search_by_hashtag(
            hashtag,
            limit=int(args.get("limit") or 50),
        )
    except ApifyError as e:
        return _surface_apify(e)
    posts = [_normalise_ig_post(i) for i in items]
    creators = _uniq_by_handle(posts)
    return ToolOutcome(
        content=(
            f"Instagram #{hashtag.lstrip('#')}: {len(posts)} posts → "
            f"{len(creators)} unique creator(s)."
        ),
        data={"hashtag": hashtag, "posts": posts, "creators": creators},
    )


ugc_instagram_hashtag_search_tool = Tool(
    name="ugc_instagram_hashtag_search",
    description=(
        "Discover Instagram creators by hashtag. Returns recent posts "
        "with captions + engagement + creator handle, plus a "
        "deduplicated list of unique creators. Feed a creator's handle "
        "into ugc_instagram_profile for full profile + bio (where the "
        "email often lives)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "hashtag": {
                "type": "string",
                "description": "Hashtag without the #. e.g. 'skincare'.",
            },
            "limit": {
                "type": "integer",
                "minimum": 10,
                "maximum": 500,
                "description": "Max posts to pull (default 50).",
            },
        },
        "required": ["hashtag"],
    },
    risk=RiskClass.NET_READ,
    handler=_ig_hashtag_search,
)


async def _ig_profile(args: dict, _ctx: ToolContext) -> ToolOutcome:
    username = str(args.get("username") or "").strip()
    if not username:
        return ToolOutcome(
            content="ugc_instagram_profile requires 'username'.",
            is_error=True,
        )
    client, err = _apify()
    if err:
        return ToolOutcome(content=err, is_error=True)
    try:
        item = await client.instagram_profile(
            username,
            post_limit=int(args.get("post_limit") or 12),
        )
    except ApifyError as e:
        return _surface_apify(e)
    if item is None:
        return ToolOutcome(
            content=f"@{username.lstrip('@')}: no profile returned "
                    "(deleted, private, or typo).",
            is_error=True,
        )
    creator = _normalise_ig_profile(item)
    return ToolOutcome(
        content=(
            f"@{creator['handle']}: {creator.get('followers') or 'n/a'} "
            f"followers, {len(creator.get('recent_posts') or [])} recent "
            f"posts. Bio email: {creator.get('bio_email') or 'none'}."
        ),
        data={"creator": creator},
    )


ugc_instagram_profile_tool = Tool(
    name="ugc_instagram_profile",
    description=(
        "Fetch an Instagram creator's full profile — follower count, "
        "bio, external link, and recent posts with captions + "
        "engagement. Auto-extracts an email from the bio if present. "
        "Agent uses this to score a candidate creator against the brief."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "username": {
                "type": "string",
                "description": "Instagram handle (with or without @).",
            },
            "post_limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Recent posts to include (default 12).",
            },
        },
        "required": ["username"],
    },
    risk=RiskClass.NET_READ,
    handler=_ig_profile,
)


async def _tt_hashtag_search(args: dict, _ctx: ToolContext) -> ToolOutcome:
    hashtag = str(args.get("hashtag") or "").strip()
    if not hashtag:
        return ToolOutcome(
            content="ugc_tiktok_hashtag_search requires 'hashtag'.",
            is_error=True,
        )
    client, err = _apify()
    if err:
        return ToolOutcome(content=err, is_error=True)
    try:
        items = await client.tiktok_search_by_hashtag(
            hashtag,
            limit=int(args.get("limit") or 50),
        )
    except ApifyError as e:
        return _surface_apify(e)
    normalised = [_normalise_tiktok_item(i) for i in items]
    # Unique-by-handle; first video per creator wins.
    seen: dict[str, dict[str, Any]] = {}
    for r in normalised:
        h = (r.get("handle") or "").lower()
        if not h:
            continue
        seen.setdefault(h, r)
    return ToolOutcome(
        content=(
            f"TikTok #{hashtag.lstrip('#')}: {len(normalised)} videos → "
            f"{len(seen)} unique creator(s)."
        ),
        data={
            "hashtag": hashtag,
            "videos": normalised,
            "creators": list(seen.values()),
        },
    )


ugc_tiktok_hashtag_search_tool = Tool(
    name="ugc_tiktok_hashtag_search",
    description=(
        "Discover TikTok creators by hashtag. Returns recent videos "
        "with captions + plays/likes/shares/comments and the creator's "
        "handle + follower count + bio. Use ugc_tiktok_profile for a "
        "deeper pull of one creator's recent work."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "hashtag": {"type": "string"},
            "limit": {
                "type": "integer",
                "minimum": 10,
                "maximum": 500,
            },
        },
        "required": ["hashtag"],
    },
    risk=RiskClass.NET_READ,
    handler=_tt_hashtag_search,
)


async def _tt_profile(args: dict, _ctx: ToolContext) -> ToolOutcome:
    username = str(args.get("username") or "").strip()
    if not username:
        return ToolOutcome(
            content="ugc_tiktok_profile requires 'username'.",
            is_error=True,
        )
    client, err = _apify()
    if err:
        return ToolOutcome(content=err, is_error=True)
    try:
        items = await client.tiktok_profile(
            username,
            post_limit=int(args.get("post_limit") or 12),
        )
    except ApifyError as e:
        return _surface_apify(e)
    if not items:
        return ToolOutcome(
            content=f"@{username.lstrip('@')}: no videos returned.",
            is_error=True,
        )
    normalised = [_normalise_tiktok_item(i) for i in items]
    first = normalised[0]
    creator = {
        **{k: v for k, v in first.items() if k != "post"},
        "videos": [n["post"] for n in normalised],
    }
    return ToolOutcome(
        content=(
            f"@{creator['handle']}: {creator.get('followers') or 'n/a'} "
            f"followers, {len(creator['videos'])} recent videos. Bio "
            f"email: {creator.get('bio_email') or 'none'}."
        ),
        data={"creator": creator},
    )


ugc_tiktok_profile_tool = Tool(
    name="ugc_tiktok_profile",
    description=(
        "Fetch a TikTok creator's recent videos and profile bio. Used "
        "after a hashtag search to get a fuller picture of a candidate."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "username": {"type": "string"},
            "post_limit": {
                "type": "integer", "minimum": 1, "maximum": 50,
            },
        },
        "required": ["username"],
    },
    risk=RiskClass.NET_READ,
    handler=_tt_profile,
)


# ── Email finder ─────────────────────────────────────────────────


async def _find_email(args: dict, _ctx: ToolContext) -> ToolOutcome:
    """Two strategies in order: if a bio is passed, regex it first
    (free, often accurate). Fall back to Hunter.io with whatever
    domain + name info the caller can provide. Both can be absent —
    the tool just reports what it could find."""
    bio = str(args.get("bio") or "")
    domain = str(args.get("domain") or "").strip()
    first_name = str(args.get("first_name") or "").strip()
    last_name = str(args.get("last_name") or "").strip()
    full_name = str(args.get("full_name") or "").strip()

    bio_hit = _extract_bio_email(bio) if bio else None
    if bio_hit:
        return ToolOutcome(
            content=f"Bio email: {bio_hit}",
            data={"email": bio_hit, "source": "bio", "confidence": 0.95},
        )

    if not domain:
        return ToolOutcome(
            content=(
                "No email in bio; pass 'domain' (and optionally "
                "first_name / last_name) to try Hunter.io email-finder."
            ),
            is_error=True,
        )

    client, err = _hunter()
    if err:
        return ToolOutcome(content=err, is_error=True)
    try:
        payload = await client.email_finder(
            domain,
            first_name=first_name or None,
            last_name=last_name or None,
            full_name=full_name or None,
        )
    except HunterError as e:
        return _surface_hunter(e)
    data = payload.get("data") or {}
    email = data.get("email")
    if not email:
        return ToolOutcome(
            content=(
                f"No confident email on {domain}. Hunter score: "
                f"{data.get('score') or 'n/a'}."
            ),
            data={"email": None, "source": "hunter", "raw": data},
        )
    confidence = (data.get("score") or 0) / 100
    return ToolOutcome(
        content=(
            f"Hunter email: {email} (confidence "
            f"{int(confidence * 100)}%, {data.get('verification', {}).get('status')})"
        ),
        data={
            "email": email,
            "source": "hunter",
            "confidence": confidence,
            "raw": data,
        },
    )


ugc_find_email_tool = Tool(
    name="ugc_find_email",
    description=(
        "Find a creator's contact email. Two strategies in order: "
        "(1) regex the bio string — catches creators who list email "
        "openly, no external call; (2) fall back to Hunter.io's email-"
        "finder with {domain, first_name/last_name | full_name}. "
        "Returns {email, source, confidence}. Pass `bio` alone for "
        "bio-only; pass `domain` for the fallback; pass both for "
        "bio-first + Hunter-if-missing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bio": {"type": "string"},
            "domain": {"type": "string"},
            "first_name": {"type": "string"},
            "last_name": {"type": "string"},
            "full_name": {"type": "string"},
        },
    },
    risk=RiskClass.NET_READ,
    handler=_find_email,
)


# ── CSV export ───────────────────────────────────────────────────


UGC_CSV_COLUMNS = (
    "handle",
    "platform",
    "followers",
    "score_overall",
    "score_quality",
    "score_brand_fit",
    "score_business_utility",
    "score_virality",
    "score_cringe_risk",
    "email",
    "email_source",
    "email_confidence",
    "profile_url",
    "top_post_url",
    "notes",
)


def _workspace_root(ctx: ToolContext) -> Path:
    return (
        ctx.sandbox_root.expanduser().resolve()
        if ctx.sandbox_root is not None
        else get_settings().workspace_dir.expanduser().resolve()
    )


async def _export_csv(args: dict, ctx: ToolContext) -> ToolOutcome:
    rel = str(args.get("path") or "").strip()
    creators = args.get("creators")
    if not rel:
        return ToolOutcome(
            content="ugc_export_csv requires 'path' (workspace-relative, "
                    "e.g. 'ugc/skincare-shortlist.csv').",
            is_error=True,
        )
    if not isinstance(creators, list) or not creators:
        return ToolOutcome(
            content="ugc_export_csv requires a non-empty 'creators' list.",
            is_error=True,
        )
    if not rel.endswith(".csv"):
        rel = rel + ".csv"
    root = _workspace_root(ctx)
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return ToolOutcome(
            content=f"path escapes workspace: {rel}", is_error=True,
        )
    candidate.parent.mkdir(parents=True, exist_ok=True)
    with candidate.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(UGC_CSV_COLUMNS))
        writer.writeheader()
        for c in creators:
            if not isinstance(c, dict):
                continue
            writer.writerow({k: c.get(k, "") for k in UGC_CSV_COLUMNS})
    return ToolOutcome(
        content=(
            f"Wrote {len(creators)} creator(s) → {rel}. Open with any "
            "spreadsheet tool; the sheet columns are intentionally "
            "aligned to the operator's standard shortlist."
        ),
        data={"path": rel, "rows": len(creators), "columns": list(UGC_CSV_COLUMNS)},
    )


ugc_export_csv_tool = Tool(
    name="ugc_export_csv",
    description=(
        "Write a deduplicated shortlist of creators to a CSV in the "
        "workspace. Standard columns: handle, platform, followers, "
        "the 5 rubric scores + overall, email + source + confidence, "
        "profile_url, top_post_url, notes. Hand the resulting path "
        "to the operator; they import it into whichever sheet they "
        "keep the creator pool in."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative CSV path, e.g. "
                               "'ugc/skincare-shortlist.csv'.",
            },
            "creators": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "Shortlist rows. Each row should carry the columns "
                    "listed in the tool output; missing keys export "
                    "blank."
                ),
            },
        },
        "required": ["path", "creators"],
    },
    risk=RiskClass.WRITE_LOCAL,
    handler=_export_csv,
)


# ── Bundle ───────────────────────────────────────────────────────


UGC_TOOLS: list[Tool] = [
    ugc_instagram_hashtag_search_tool,
    ugc_instagram_profile_tool,
    ugc_tiktok_hashtag_search_tool,
    ugc_tiktok_profile_tool,
    ugc_find_email_tool,
    ugc_export_csv_tool,
]
