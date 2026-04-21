"""Sales-ops toolkit: prospecting, site audits, email enrichment.

Drop-in tool pack for the ``sales_ops_agent``. Every tool here is a
thin httpx wrapper around a third-party API — no local state, no
sandbox side-effects. When the underlying key isn't configured the
handler returns a clean ``is_error`` outcome instead of raising, so
the agent can recover (or surface "connect this integration in
Settings").

Risk classes:
    google_places_search, site_audit, hunter_* → NET_READ  (outbound GET)

CRM operations (contacts, opportunities, notes) live in the Go High
Level toolkit at ``core.integrations.ghl``. HubSpot was the v1 CRM
backend; it was removed in PR #75c when GHL shipped with broader
coverage (pipelines, conversations, calendars, workflows). The two
coexisted briefly during the rollout — today there is only GHL.
"""

from __future__ import annotations

from typing import Any

import httpx

from core.config import get_settings
from core.policy.risk import RiskClass
from core.secrets import resolve_secret
from core.tools.registry import Tool, ToolContext, ToolOutcome

DEFAULT_TIMEOUT_S = 20.0


def _secret(name: str, fallback: str | None) -> str | None:
    """User-set secret from the dashboard wins; fall back to env var."""
    return resolve_secret(name, fallback)


# ── Google Maps / Places ──────────────────────────────────────────

async def _google_places_search(
    args: dict, ctx: ToolContext
) -> ToolOutcome:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolOutcome(
            content="google_places_search requires a 'query' argument.",
            is_error=True,
        )
    api_key = _secret("google_places_api_key", get_settings().google_places_api_key)
    if not api_key:
        return ToolOutcome(
            content=(
                "Google Places is not configured. Add a Google Places API "
                "key in Settings → API Keys (or set GOOGLE_PLACES_API_KEY)."
            ),
            is_error=True,
        )
    limit = int(args.get("limit") or 10)
    limit = max(1, min(limit, 20))
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            resp = await client.post(
                "https://places.googleapis.com/v1/places:searchText",
                headers={
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": (
                        "places.id,places.displayName,"
                        "places.formattedAddress,places.websiteUri,"
                        "places.nationalPhoneNumber,places.rating,"
                        "places.userRatingCount"
                    ),
                    "Content-Type": "application/json",
                },
                json={"textQuery": query, "pageSize": limit},
            )
    except (httpx.HTTPError, TimeoutError) as e:
        return ToolOutcome(
            content=f"google_places_search failed: {type(e).__name__}: {e}",
            is_error=True,
        )
    if resp.status_code >= 400:
        return ToolOutcome(
            content=(
                f"Google Places error {resp.status_code}: "
                f"{resp.text[:500]}"
            ),
            is_error=True,
            data={"status": resp.status_code},
        )
    payload = resp.json()
    places = payload.get("places") or []
    simplified = [
        {
            "place_id": p.get("id"),
            "name": (p.get("displayName") or {}).get("text"),
            "address": p.get("formattedAddress"),
            "website": p.get("websiteUri"),
            "phone": p.get("nationalPhoneNumber"),
            "rating": p.get("rating"),
            "reviews": p.get("userRatingCount"),
        }
        for p in places
    ]
    summary = "\n".join(
        f"- {s['name']} — {s['website'] or 'no website'} — {s['address']}"
        for s in simplified
    ) or "(no results)"
    return ToolOutcome(
        content=f"Found {len(simplified)} place(s) for '{query}':\n{summary}",
        data={"query": query, "results": simplified},
    )


google_places_search_tool = Tool(
    name="google_places_search",
    description=(
        "Search Google Places by free-text query (e.g. 'CPAs in Tampa FL'). "
        "Returns up to 20 businesses with name, address, website, phone, "
        "rating. Requires GOOGLE_PLACES_API_KEY."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text query (category + geo).",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Max results (default 10).",
            },
        },
        "required": ["query"],
    },
    risk=RiskClass.NET_READ,
    handler=_google_places_search,
)


# ── Site audit (PageSpeed Insights) ───────────────────────────────

async def _site_audit(args: dict, ctx: ToolContext) -> ToolOutcome:
    url = str(args.get("url") or "").strip()
    if not url:
        return ToolOutcome(
            content="site_audit requires a 'url' argument.",
            is_error=True,
        )
    if not (url.startswith("http://") or url.startswith("https://")):
        url = f"https://{url}"
    api_key = _secret("pagespeed_api_key", get_settings().pagespeed_api_key)
    if not api_key:
        return ToolOutcome(
            content=(
                "PageSpeed is not configured. Add a PageSpeed Insights "
                "API key in Settings → API Keys (or set "
                "PAGESPEED_API_KEY)."
            ),
            is_error=True,
        )
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                params={
                    "url": url,
                    "strategy": args.get("strategy") or "mobile",
                    "category": "performance",
                    "key": api_key,
                },
            )
    except (httpx.HTTPError, TimeoutError) as e:
        return ToolOutcome(
            content=f"site_audit failed: {type(e).__name__}: {e}",
            is_error=True,
        )
    if resp.status_code >= 400:
        return ToolOutcome(
            content=f"PageSpeed error {resp.status_code}: {resp.text[:500]}",
            is_error=True,
            data={"status": resp.status_code},
        )
    payload = resp.json()
    lighthouse = payload.get("lighthouseResult") or {}
    categories = lighthouse.get("categories") or {}
    perf = (categories.get("performance") or {}).get("score")
    audits = lighthouse.get("audits") or {}
    def _metric(key: str) -> Any:
        return (audits.get(key) or {}).get("displayValue")
    ssl_ok = url.startswith("https://")
    # "Bad-site" score: inverted performance (0 = perfect, 100 = broken)
    # plus a nudge for no-HTTPS. Cheap heuristic good enough for v1.
    bad_score = 0
    if perf is not None:
        bad_score = round((1 - float(perf)) * 100)
    if not ssl_ok:
        bad_score = min(100, bad_score + 10)
    summary = {
        "url": url,
        "performance_score": perf,
        "bad_site_score": bad_score,
        "ssl": ssl_ok,
        "lcp": _metric("largest-contentful-paint"),
        "cls": _metric("cumulative-layout-shift"),
        "tbt": _metric("total-blocking-time"),
        "fcp": _metric("first-contentful-paint"),
    }
    return ToolOutcome(
        content=(
            f"Audit for {url}: bad_site_score={bad_score}/100 "
            f"(performance={perf}, ssl={ssl_ok}, "
            f"lcp={summary['lcp']}, cls={summary['cls']})."
        ),
        data=summary,
    )


site_audit_tool = Tool(
    name="site_audit",
    description=(
        "Score a prospect website 0-100 on how 'dated/broken' it looks. "
        "Uses Google PageSpeed Insights (performance category) plus an SSL "
        "nudge. Higher = worse. Good first filter before reaching out. "
        "Requires PAGESPEED_API_KEY."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Target site URL (bare domain OK).",
            },
            "strategy": {
                "type": "string",
                "enum": ["mobile", "desktop"],
                "description": "Audit strategy (default mobile).",
            },
        },
        "required": ["url"],
    },
    risk=RiskClass.NET_READ,
    handler=_site_audit,
)


# ── Hunter.io email finder ────────────────────────────────────────

async def _hunter_find_email(args: dict, ctx: ToolContext) -> ToolOutcome:
    domain = str(args.get("domain") or "").strip()
    if not domain:
        return ToolOutcome(
            content="hunter_find_email requires a 'domain' argument.",
            is_error=True,
        )
    api_key = _secret("hunter_io_api_key", get_settings().hunter_io_api_key)
    if not api_key:
        return ToolOutcome(
            content=(
                "Hunter.io is not configured. Add a Hunter.io API key in "
                "Settings → API Keys (or set HUNTER_IO_API_KEY)."
            ),
            is_error=True,
        )
    first = args.get("first_name") or None
    last = args.get("last_name") or None
    endpoint = (
        "email-finder" if (first or last) else "domain-search"
    )
    params: dict[str, Any] = {"domain": domain, "api_key": api_key}
    if first:
        params["first_name"] = first
    if last:
        params["last_name"] = last
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            resp = await client.get(
                f"https://api.hunter.io/v2/{endpoint}", params=params,
            )
    except (httpx.HTTPError, TimeoutError) as e:
        return ToolOutcome(
            content=f"hunter_find_email failed: {type(e).__name__}: {e}",
            is_error=True,
        )
    if resp.status_code >= 400:
        return ToolOutcome(
            content=f"Hunter.io error {resp.status_code}: {resp.text[:500]}",
            is_error=True,
            data={"status": resp.status_code},
        )
    data = resp.json().get("data") or {}
    if endpoint == "email-finder":
        email = data.get("email")
        score = data.get("score")
        return ToolOutcome(
            content=(
                f"Best guess: {email or 'not found'} "
                f"(confidence {score})" if email
                else f"No email found for {first or ''} {last or ''} @ {domain}."
            ),
            data={"email": email, "score": score, "domain": domain},
        )
    emails = data.get("emails") or []
    summary = "\n".join(
        f"- {e.get('value')} — {e.get('first_name', '')} "
        f"{e.get('last_name', '')} ({e.get('position') or '?'})"
        for e in emails[:10]
    ) or "(no emails found)"
    return ToolOutcome(
        content=f"Hunter domain-search {domain}:\n{summary}",
        data={"domain": domain, "emails": emails},
    )


hunter_find_email_tool = Tool(
    name="hunter_find_email",
    description=(
        "Find emails for a domain via Hunter.io. If first_name + last_name "
        "are supplied, uses email-finder (best-guess + confidence). "
        "Otherwise runs domain-search (up to 10 public emails). Requires "
        "HUNTER_IO_API_KEY."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": "Company domain (e.g. 'acme.com').",
            },
            "first_name": {"type": "string"},
            "last_name": {"type": "string"},
        },
        "required": ["domain"],
    },
    risk=RiskClass.NET_READ,
    handler=_hunter_find_email,
)



SALES_OPS_TOOLS: list[Tool] = [
    google_places_search_tool,
    site_audit_tool,
    hunter_find_email_tool,
]
