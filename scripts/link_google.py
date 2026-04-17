"""Interactive linker for PILK's Google account.

Run once:

    python -m scripts.link_google

Needs `pilk-google-client.json` (the OAuth *client* secret you downloaded
from Google Cloud → Credentials → OAuth 2.0 Client ID → Desktop) at the
repo root, or wherever PILK_GOOGLE_CLIENT_SECRET points.

Opens a browser, asks you to sign in as PILK's Gmail account, then
writes the refresh token to ~/PILK/identity/integrations/google.json.
That file replaces any prior link on disk.
"""

from __future__ import annotations

import contextlib
import json
import sys
from datetime import UTC, datetime

from core.config import get_settings
from core.integrations.google.oauth import DEFAULT_SCOPES


def _fail(msg: str, code: int = 1) -> int:
    sys.stderr.write(f"link_google: {msg}\n")
    return code


def main() -> int:
    settings = get_settings()
    settings.integrations_dir.mkdir(parents=True, exist_ok=True)

    client_path = settings.google_client_secret_path
    if not client_path.is_absolute():
        # Resolve relative to repo root
        client_path = (client_path).resolve()
    if not client_path.exists():
        return _fail(
            f"client secret not found at {client_path}. "
            "Download the OAuth Desktop client JSON from Google Cloud → "
            "Credentials and save it there (or set PILK_GOOGLE_CLIENT_SECRET)."
        )

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError:
        return _fail(
            "google-auth-oauthlib isn't installed. "
            "Run `pip install google-auth google-auth-oauthlib google-api-python-client`"
        )

    print(f"Loading client secret from {client_path}")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_path), scopes=DEFAULT_SCOPES
    )
    # run_local_server pops a browser and catches the redirect locally.
    print("Opening browser for Google sign-in — pick PILK's Gmail account…")
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    if not creds or not creds.refresh_token:
        return _fail(
            "no refresh token returned. Remove access from "
            "myaccount.google.com/permissions and re-run so Google prompts "
            "for consent again."
        )

    # Resolve account email via the userinfo endpoint.
    email: str | None = None
    try:
        from googleapiclient.discovery import build  # type: ignore

        oauth2 = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        info = oauth2.userinfo().get().execute()
        email = info.get("email")
    except Exception as e:
        print(f"(couldn't fetch account email: {e}; continuing anyway)")

    out = {
        "email": email,
        "refresh_token": creds.refresh_token,
        "access_token": creds.token,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or DEFAULT_SCOPES),
        "linked_at": datetime.now(UTC).isoformat(),
    }
    target = settings.google_credentials_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, indent=2))
    with contextlib.suppress(Exception):
        target.chmod(0o600)

    print()
    print(f"✓ Linked {email or '(unknown email)'}")
    print(f"  scopes: {', '.join(out['scopes'])}")
    print(f"  stored at {target}")
    print()
    print("Restart pilkd to pick up the new Gmail tools:")
    print("  python -m core.main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
