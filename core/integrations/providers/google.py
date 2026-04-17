"""Google as an OAuthProvider.

Everything Google-specific lives in this file: auth + token URLs,
scope catalog, profile fetcher. The generic OAuth flow in
`core.integrations.oauth_flow` consumes this declaratively.

System role uses send-only Gmail scope; user role additionally gets
read + modify so PILK can triage and draft on the real inbox. Drive /
Calendar scopes will extend the catalog in a later batch without
changing this file's shape.
"""

from __future__ import annotations

from core.integrations.provider import (
    OAuthProfile,
    OAuthProvider,
    Role,
    ScopeSpec,
)
from core.policy.risk import RiskClass

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# Keyed by a short PILK-internal name. `scope_uri` is what Google wants.
SCOPE_CATALOG: dict[str, ScopeSpec] = {
    "gmail.send": ScopeSpec(
        name="gmail.send",
        scope_uri="https://www.googleapis.com/auth/gmail.send",
        label="Send email",
        risk_hint=RiskClass.COMMS,
        user_voice=True,
    ),
    "gmail.modify": ScopeSpec(
        name="gmail.modify",
        scope_uri="https://www.googleapis.com/auth/gmail.modify",
        label="Modify mail (labels, read state, drafts)",
        risk_hint=RiskClass.NET_WRITE,
    ),
    "gmail.readonly": ScopeSpec(
        name="gmail.readonly",
        scope_uri="https://www.googleapis.com/auth/gmail.readonly",
        label="Read mail",
        risk_hint=RiskClass.NET_READ,
    ),
    "openid": ScopeSpec(
        name="openid",
        scope_uri="openid",
        label="Basic identity",
        risk_hint=RiskClass.READ,
    ),
    "userinfo.email": ScopeSpec(
        name="userinfo.email",
        scope_uri="https://www.googleapis.com/auth/userinfo.email",
        label="Account email address",
        risk_hint=RiskClass.READ,
    ),
    "userinfo.profile": ScopeSpec(
        name="userinfo.profile",
        scope_uri="https://www.googleapis.com/auth/userinfo.profile",
        label="Account profile (name, avatar)",
        risk_hint=RiskClass.READ,
    ),
}

_SYSTEM_SCOPE_NAMES: list[str] = [
    "gmail.send",
    "openid",
    "userinfo.email",
    "userinfo.profile",
]
_USER_SCOPE_NAMES: list[str] = [
    "gmail.send",
    "gmail.modify",
    "gmail.readonly",
    "openid",
    "userinfo.email",
    "userinfo.profile",
]


def _scopes_for_role(role: Role) -> list[str]:
    names = _USER_SCOPE_NAMES if role == "user" else _SYSTEM_SCOPE_NAMES
    return [SCOPE_CATALOG[n].scope_uri for n in names]


def _fetch_profile(tokens: dict) -> OAuthProfile:
    """Call Google userinfo with the fresh credentials, return email+name.

    Uses the same google-auth path the old link script used, so we don't
    add a new HTTP client. Failures degrade gracefully: the OAuth flow
    still completes, the account just gets no email/username until the
    user re-links.
    """
    try:
        from google.oauth2.credentials import Credentials  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except ImportError:
        return OAuthProfile(email=None, username=None)
    try:
        creds = Credentials(
            token=tokens.get("access_token"),
            refresh_token=tokens.get("refresh_token"),
            token_uri=tokens.get("token_uri", TOKEN_URL),
            client_id=tokens.get("client_id"),
            client_secret=tokens.get("client_secret"),
            scopes=list(tokens.get("scopes") or []),
        )
        service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        info = service.userinfo().get().execute() or {}
    except Exception:
        return OAuthProfile(email=None, username=None)
    return OAuthProfile(
        email=info.get("email"),
        username=info.get("name"),
        extra={"picture": info.get("picture")},
    )


google_provider = OAuthProvider(
    name="google",
    label="Google",
    auth_url=AUTH_URL,
    token_url=TOKEN_URL,
    scope_catalog=SCOPE_CATALOG,
    scopes_for_role=_scopes_for_role,
    profile_fetcher=_fetch_profile,
    supports_roles=("system", "user"),
)
