"""Google OAuth helpers — load + refresh the refresh-token blob.

One integration, one file on disk at ~/PILK/identity/integrations/google.json:

    {
      "email": "pilk@...",
      "refresh_token": "...",
      "client_id": "...",
      "client_secret": "...",
      "scopes": ["https://www.googleapis.com/auth/gmail.send", ...],
      "linked_at": "2026-04-17T..."
    }

`load_credentials` returns a live `google.oauth2.credentials.Credentials`
with auto-refresh — the Gmail/Drive/Calendar clients take it directly.
`status()` is a cheap check for the Settings UI: are we linked, what
email, what scopes.

Credentials never hit git. The file is written only by
`scripts.link_google`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.logging import get_logger

log = get_logger("pilkd.google")

# Scopes are split per role. The system account is still mail-only
# (no Drive/Calendar), but includes read+modify so PILK can complete
# verification-email flows for service-account signup tasks. The user
# account keeps the same read+modify set for triage and drafting on the
# operator's real inbox. Keeping each scope list narrow is a deliberate
# isolation boundary — we can always re-link to widen.
SYSTEM_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]
USER_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]
# Back-compat alias for callers that still import DEFAULT_SCOPES.
# Defaults to the wider scope set so re-linking from pre-Batch-K code
# doesn't accidentally downgrade permissions.
DEFAULT_SCOPES: list[str] = USER_SCOPES


@dataclass
class GoogleLinkStatus:
    linked: bool
    email: str | None = None
    scopes: list[str] | None = None
    linked_at: str | None = None
    error: str | None = None

    def to_public(self) -> dict:
        return {
            "linked": self.linked,
            "email": self.email,
            "scopes": self.scopes or [],
            "linked_at": self.linked_at,
            "error": self.error,
        }


@dataclass
class GoogleCredentials:
    """Thin wrapper so the tools don't each know the pickle path."""

    raw: Any  # google.oauth2.credentials.Credentials
    email: str | None

    def build(self, api: str, version: str):
        """Return a googleapiclient service bound to these credentials."""
        from googleapiclient.discovery import build  # type: ignore

        return build(api, version, credentials=self.raw, cache_discovery=False)


def status(credentials_path: Path) -> GoogleLinkStatus:
    """Cheap read of the link file. No network."""
    if not credentials_path.exists():
        return GoogleLinkStatus(linked=False)
    try:
        data = json.loads(credentials_path.read_text())
    except Exception as e:  # pragma: no cover
        return GoogleLinkStatus(linked=False, error=f"unreadable: {e}")
    if not data.get("refresh_token"):
        return GoogleLinkStatus(linked=False, error="no refresh_token in link file")
    return GoogleLinkStatus(
        linked=True,
        email=data.get("email"),
        scopes=list(data.get("scopes") or []),
        linked_at=data.get("linked_at"),
    )


def load_credentials(credentials_path: Path) -> GoogleCredentials | None:
    """Return live Credentials or None if not linked.

    Legacy Batch-K entrypoint — reads a per-role JSON file directly.
    New code path resolves credentials through AccountsStore and calls
    `credentials_from_blob` instead.
    """
    if not credentials_path.exists():
        return None
    try:
        data = json.loads(credentials_path.read_text())
    except Exception as e:  # pragma: no cover
        log.warning("google_credentials_unreadable", detail=str(e))
        return None
    return credentials_from_blob(data)


def credentials_from_blob(data: dict) -> GoogleCredentials | None:
    """Build a live GoogleCredentials from a token dict.

    Accepts either the legacy Batch-K file shape or the AccountsStore
    secrets shape — they share the fields we care about
    (access_token, refresh_token, client_id, client_secret, scopes).
    """
    refresh = data.get("refresh_token")
    if not refresh:
        return None
    try:
        from google.oauth2.credentials import Credentials  # type: ignore

        creds = Credentials(
            token=data.get("access_token"),
            refresh_token=refresh,
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=list(data.get("scopes") or DEFAULT_SCOPES),
        )
    except Exception as e:  # pragma: no cover — SDK missing
        log.warning("google_sdk_missing", detail=str(e))
        return None
    return GoogleCredentials(raw=creds, email=data.get("email"))
