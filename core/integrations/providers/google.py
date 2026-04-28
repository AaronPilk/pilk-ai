"""Google as an OAuthProvider.

Everything Google-specific lives in this file: auth + token URLs,
scope catalog, profile fetcher. The generic OAuth flow in
`core.integrations.oauth_flow` consumes this declaratively.

The scope catalog covers Gmail, Drive, and Calendar. Which scopes get
requested at OAuth time depends on the *groups* the UI opts into —
`mail` is the default, `drive` and `calendar` add on top. This lets
the user re-link an existing account to widen access without baking
the kitchen sink into every connection.

System role is deliberately limited to mail-only by default (no Drive
or Calendar), but includes Gmail read/modify so PILK can complete
service-account workflows that depend on inbox verification links
(e.g. account signup + email confirm) using PILK's own mailbox.
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
    "drive.readonly": ScopeSpec(
        name="drive.readonly",
        scope_uri="https://www.googleapis.com/auth/drive.readonly",
        label="Read Drive files",
        risk_hint=RiskClass.NET_READ,
    ),
    "calendar.readonly": ScopeSpec(
        name="calendar.readonly",
        scope_uri="https://www.googleapis.com/auth/calendar.readonly",
        label="Read calendar events",
        risk_hint=RiskClass.NET_READ,
    ),
    "calendar.events": ScopeSpec(
        name="calendar.events",
        scope_uri="https://www.googleapis.com/auth/calendar.events",
        label="Create or modify calendar events",
        risk_hint=RiskClass.NET_WRITE,
    ),
    "slides.edit": ScopeSpec(
        name="slides.edit",
        scope_uri="https://www.googleapis.com/auth/presentations",
        label="Create or edit Google Slides presentations",
        risk_hint=RiskClass.NET_WRITE,
    ),
    "sheets.edit": ScopeSpec(
        name="sheets.edit",
        scope_uri="https://www.googleapis.com/auth/spreadsheets",
        label="Create or edit Google Sheets",
        risk_hint=RiskClass.NET_WRITE,
    ),
    "drive.file": ScopeSpec(
        # Scoped access to only the files the app creates — required
        # for Slides because creating a presentation also creates a
        # Drive file, and Slides API uses drive.file to return the
        # deck URL.
        name="drive.file",
        scope_uri="https://www.googleapis.com/auth/drive.file",
        label="Access to files this app creates",
        risk_hint=RiskClass.NET_WRITE,
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

# Every sign-in includes these so we can identify the account.
_BASE_SCOPE_NAMES: list[str] = ["openid", "userinfo.email", "userinfo.profile"]

# Per-group scope lists, split by role where the answer differs.
_GROUP_SCOPES_SYSTEM: dict[str, list[str]] = {
    "mail": ["gmail.send", "gmail.modify", "gmail.readonly"],
}
_GROUP_SCOPES_USER: dict[str, list[str]] = {
    "mail": ["gmail.send", "gmail.modify", "gmail.readonly"],
    "drive": ["drive.readonly"],
    "calendar": ["calendar.readonly", "calendar.events"],
    # Slides needs presentations + drive.file (Slides API creates the
    # deck via Drive's file metadata layer).
    "slides": ["slides.edit", "drive.file"],
    # Sheets mirrors Slides — spreadsheets + drive.file so the sheet
    # lands as a real Drive file with a shareable URL.
    "sheets": ["sheets.edit", "drive.file"],
}

# UI metadata: group name → human label for the Expand-access modal.
SCOPE_GROUP_LABELS: dict[str, str] = {
    "mail": "Mail",
    "drive": "Drive",
    "calendar": "Calendar",
    "slides": "Slides",
    "sheets": "Sheets",
}


def _scopes_for_role(role: Role, groups: list[str] | None = None) -> list[str]:
    active = [g for g in (groups or ("mail",)) if g]
    per_role = _GROUP_SCOPES_USER if role == "user" else _GROUP_SCOPES_SYSTEM
    names: set[str] = set(_BASE_SCOPE_NAMES)
    for g in active:
        names.update(per_role.get(g, []))
    return [SCOPE_CATALOG[n].scope_uri for n in sorted(names)]


def _fetch_profile(tokens: dict) -> OAuthProfile:
    """Call Google userinfo with the fresh credentials, return email+name."""
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
    scope_groups=dict(SCOPE_GROUP_LABELS),
    default_scope_groups=("mail",),
    supports_roles=("system", "user"),
)
