"""Meta (Facebook + Instagram) as an OAuthProvider.

One Meta app, one OAuth flow, two capabilities:

- Facebook Pages posting. Requires a Page you manage; personal-profile
  posting was removed from the Graph API in 2018 and is not coming
  back. Scopes: pages_manage_posts, pages_read_engagement,
  pages_show_list.
- Instagram Business/Creator publishing. Requires an IG Business or
  Creator account linked to a Facebook Page. Scopes: instagram_basic,
  instagram_content_publish (plus the Page scopes since IG publish
  routes through the connected Page).

Personal Facebook wall and personal Instagram accounts do NOT work
here — Meta's API doesn't allow it, no matter how many scopes you
request. The UI tiles label themselves "Facebook Page" and
"Instagram Business" to make that honest.

Access tokens are long-lived (~60 days) but don't come with refresh
tokens in the standard flow; `requires_refresh_token=False`. Meta
surfaces token expiry in the Connected accounts status pill.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from core.integrations.provider import (
    OAuthProfile,
    OAuthProvider,
    Role,
    ScopeSpec,
)
from core.policy.risk import RiskClass

AUTH_URL = "https://www.facebook.com/v20.0/dialog/oauth"
TOKEN_URL = "https://graph.facebook.com/v20.0/oauth/access_token"
ME_URL = "https://graph.facebook.com/v20.0/me"
ACCOUNTS_URL = "https://graph.facebook.com/v20.0/me/accounts"

SCOPE_CATALOG: dict[str, ScopeSpec] = {
    "email": ScopeSpec(
        name="email",
        scope_uri="email",
        label="Your Meta email",
        risk_hint=RiskClass.READ,
    ),
    "public_profile": ScopeSpec(
        name="public_profile",
        scope_uri="public_profile",
        label="Basic Meta profile",
        risk_hint=RiskClass.READ,
    ),
    "pages_show_list": ScopeSpec(
        name="pages_show_list",
        scope_uri="pages_show_list",
        label="List the Pages you manage",
        risk_hint=RiskClass.NET_READ,
    ),
    "pages_read_engagement": ScopeSpec(
        name="pages_read_engagement",
        scope_uri="pages_read_engagement",
        label="Read Page metadata + engagement",
        risk_hint=RiskClass.NET_READ,
    ),
    "pages_manage_posts": ScopeSpec(
        name="pages_manage_posts",
        scope_uri="pages_manage_posts",
        label="Post on Pages you manage",
        risk_hint=RiskClass.COMMS,
        user_voice=True,
    ),
    "instagram_basic": ScopeSpec(
        name="instagram_basic",
        scope_uri="instagram_basic",
        label="Read your Instagram Business profile",
        risk_hint=RiskClass.NET_READ,
    ),
    "instagram_content_publish": ScopeSpec(
        name="instagram_content_publish",
        scope_uri="instagram_content_publish",
        label="Publish on your Instagram Business account",
        risk_hint=RiskClass.COMMS,
        user_voice=True,
    ),
}

_BASE_NAMES: list[str] = ["public_profile", "email"]

_GROUP_SCOPES: dict[str, list[str]] = {
    "pages": [
        "pages_show_list",
        "pages_read_engagement",
        "pages_manage_posts",
    ],
    "instagram": [
        # IG publish routes through the connected Page, so we add the
        # Page-listing + engagement scopes alongside the IG-specific ones.
        "pages_show_list",
        "pages_read_engagement",
        "instagram_basic",
        "instagram_content_publish",
    ],
}

SCOPE_GROUP_LABELS: dict[str, str] = {
    "pages": "Facebook Page posting",
    "instagram": "Instagram Business publishing",
}


def _scopes_for_role(role: Role, groups: list[str] | None = None) -> list[str]:
    if role != "user":
        return []
    active = [g for g in (groups or ("pages",)) if g]
    names: set[str] = set(_BASE_NAMES)
    for g in active:
        names.update(_GROUP_SCOPES.get(g, []))
    return [SCOPE_CATALOG[n].scope_uri for n in sorted(names)]


def _fetch_profile(tokens: dict) -> OAuthProfile:
    """Fetch identity + stash managed Pages + linked IG accounts in extra.

    We call /me for the basic profile, then /me/accounts to discover
    Pages the user manages and any linked Instagram Business accounts.
    The post tools read this out of `account.extra` so they don't have
    to re-discover on every call.
    """
    access_token = tokens.get("access_token")
    if not access_token:
        return OAuthProfile(email=None, username=None)
    profile: dict = {}
    try:
        profile_url = (
            f"{ME_URL}?fields=id,name,email&access_token="
            f"{urllib.parse.quote(access_token)}"
        )
        with urllib.request.urlopen(profile_url, timeout=10) as resp:
            profile = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        profile = {}
    pages: list[dict] = []
    try:
        accounts_url = (
            f"{ACCOUNTS_URL}?fields=id,name,access_token,"
            f"instagram_business_account&access_token="
            f"{urllib.parse.quote(access_token)}"
        )
        with urllib.request.urlopen(accounts_url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        for p in data.get("data") or []:
            ig = p.get("instagram_business_account") or {}
            pages.append(
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "page_access_token": p.get("access_token"),
                    "ig_business_id": ig.get("id"),
                }
            )
    except Exception:
        pages = []
    return OAuthProfile(
        email=profile.get("email"),
        username=profile.get("name"),
        extra={
            "meta_user_id": profile.get("id"),
            "pages": pages,
            "has_page": any(p.get("id") for p in pages),
            "has_instagram": any(p.get("ig_business_id") for p in pages),
        },
    )


meta_provider = OAuthProvider(
    name="meta",
    label="Meta",
    auth_url=AUTH_URL,
    token_url=TOKEN_URL,
    scope_catalog=SCOPE_CATALOG,
    scopes_for_role=_scopes_for_role,
    profile_fetcher=_fetch_profile,
    scope_groups=dict(SCOPE_GROUP_LABELS),
    default_scope_groups=("pages",),
    supports_roles=("user",),
    extra_auth_params={},
    requires_refresh_token=False,
)
