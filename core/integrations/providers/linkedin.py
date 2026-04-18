"""LinkedIn as an OAuthProvider.

Uses the OpenID-Connect flavor of LinkedIn's OAuth 2.0:

- Auth URL: https://www.linkedin.com/oauth/v2/authorization
- Token URL: https://www.linkedin.com/oauth/v2/accessToken
- Profile: https://api.linkedin.com/v2/userinfo (OIDC userinfo)

Access tokens are long-lived (~60 days). LinkedIn only hands out
refresh_tokens to apps with the Marketing Developer Platform add-on,
which most apps don't have — so `requires_refresh_token=False`.
A periodic re-link is the trade-off; it's surfaced as "Needs re-auth"
in the Connected accounts card when the token expires.

User role only in this batch. Company page posts are deferred.
"""

from __future__ import annotations

import json
import urllib.request

from core.integrations.provider import (
    OAuthProfile,
    OAuthProvider,
    Role,
    ScopeSpec,
)
from core.policy.risk import RiskClass

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"

SCOPE_CATALOG: dict[str, ScopeSpec] = {
    "openid": ScopeSpec(
        name="openid",
        scope_uri="openid",
        label="Basic identity",
        risk_hint=RiskClass.READ,
    ),
    "profile": ScopeSpec(
        name="profile",
        scope_uri="profile",
        label="Your LinkedIn profile (name, headline)",
        risk_hint=RiskClass.READ,
    ),
    "email": ScopeSpec(
        name="email",
        scope_uri="email",
        label="Your LinkedIn email address",
        risk_hint=RiskClass.READ,
    ),
    "w_member_social": ScopeSpec(
        name="w_member_social",
        scope_uri="w_member_social",
        label="Post on LinkedIn as you",
        risk_hint=RiskClass.COMMS,
        user_voice=True,
    ),
}

_GROUP_SCOPES: dict[str, list[str]] = {
    "identity": ["openid", "profile", "email"],
    "posts": ["w_member_social"],
}

SCOPE_GROUP_LABELS: dict[str, str] = {
    "identity": "Identity",
    "posts": "Post as you",
}


def _scopes_for_role(role: Role, groups: list[str] | None = None) -> list[str]:
    if role != "user":
        return []
    active = [g for g in (groups or ("identity", "posts")) if g]
    names: set[str] = set()
    for g in active:
        names.update(_GROUP_SCOPES.get(g, []))
    # identity is always included so we can fetch the profile after auth.
    names.update(_GROUP_SCOPES["identity"])
    return [SCOPE_CATALOG[n].scope_uri for n in sorted(names)]


def _fetch_profile(tokens: dict) -> OAuthProfile:
    access_token = tokens.get("access_token")
    if not access_token:
        return OAuthProfile(email=None, username=None)
    try:
        req = urllib.request.Request(
            USERINFO_URL,
            method="GET",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return OAuthProfile(email=None, username=None)
    return OAuthProfile(
        email=data.get("email"),
        username=data.get("name"),
        extra={
            "linkedin_sub": data.get("sub"),
            "picture": data.get("picture"),
        },
    )


linkedin_provider = OAuthProvider(
    name="linkedin",
    label="LinkedIn",
    auth_url=AUTH_URL,
    token_url=TOKEN_URL,
    scope_catalog=SCOPE_CATALOG,
    scopes_for_role=_scopes_for_role,
    profile_fetcher=_fetch_profile,
    scope_groups=dict(SCOPE_GROUP_LABELS),
    default_scope_groups=("identity", "posts"),
    supports_roles=("user",),
    extra_auth_params={},
    requires_refresh_token=False,
)
