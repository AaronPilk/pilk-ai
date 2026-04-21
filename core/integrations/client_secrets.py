"""Provider → (client_id, client_secret) resolver used by the OAuth flow.

Keeps filesystem reads and env lookups out of the generic flow module.
Returns None when no client is configured for a given provider; the
caller surfaces a clear error.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def load_client(provider: str, *, settings) -> tuple[str, str] | None:
    """Return (client_id, client_secret) for `provider`, or None."""
    if provider == "google":
        return _load_google_client(settings.google_client_secret_path)
    if provider == "slack":
        return _load_env_pair("PILK_SLACK_CLIENT_ID", "PILK_SLACK_CLIENT_SECRET")
    if provider == "linkedin":
        return _load_env_pair(
            "PILK_LINKEDIN_CLIENT_ID", "PILK_LINKEDIN_CLIENT_SECRET"
        )
    if provider == "x":
        return _load_env_pair("PILK_X_CLIENT_ID", "PILK_X_CLIENT_SECRET")
    if provider == "meta":
        return _load_env_pair("PILK_META_CLIENT_ID", "PILK_META_CLIENT_SECRET")
    return None


def is_configured(provider: str, *, settings) -> bool:
    """Whether OAuth client credentials are loadable for `provider`."""
    return load_client(provider, settings=settings) is not None


def setup_hint(provider: str, *, settings) -> str | None:
    """One-line human instruction for wiring the provider's OAuth client.

    Used by the UI to replace the generic "not configured" dead-end with
    an actionable next step, and embedded in the RuntimeError the OAuth
    flow raises when `start` is invoked on an unconfigured provider.
    """
    if provider == "google":
        path = getattr(settings, "google_client_secret_path", "pilk-google-client.json")
        return (
            f"Place a Google Cloud Desktop OAuth client JSON at `{path}`, "
            "or set PILK_GOOGLE_CLIENT_ID + PILK_GOOGLE_CLIENT_SECRET."
        )
    if provider == "slack":
        return "Set PILK_SLACK_CLIENT_ID + PILK_SLACK_CLIENT_SECRET."
    if provider == "linkedin":
        return "Set PILK_LINKEDIN_CLIENT_ID + PILK_LINKEDIN_CLIENT_SECRET."
    if provider == "x":
        return "Set PILK_X_CLIENT_ID + PILK_X_CLIENT_SECRET."
    if provider == "meta":
        return "Set PILK_META_CLIENT_ID + PILK_META_CLIENT_SECRET."
    return None


def _load_env_pair(id_var: str, secret_var: str) -> tuple[str, str] | None:
    cid = os.getenv(id_var)
    csec = os.getenv(secret_var)
    if cid and csec:
        return (cid, csec)
    return None


def _load_google_client(path: Path) -> tuple[str, str] | None:
    """Read pilk-google-client.json (Google Cloud Desktop OAuth client)."""
    candidates: list[Path] = [path]
    if not path.is_absolute():
        candidates.append(Path.cwd() / path)
    real = next((p for p in candidates if p.exists()), None)
    if real is None:
        # Env-var fallback for advanced setups.
        env_id = os.getenv("PILK_GOOGLE_CLIENT_ID")
        env_secret = os.getenv("PILK_GOOGLE_CLIENT_SECRET")
        if env_id and env_secret:
            return (env_id, env_secret)
        return None
    try:
        data = json.loads(real.read_text())
    except Exception:
        return None
    # Desktop client: {"installed": {"client_id": "...", "client_secret": "..."}}
    # Web client:     {"web":       {...}}
    for key in ("installed", "web"):
        info = data.get(key)
        if isinstance(info, dict) and info.get("client_id") and info.get("client_secret"):
            return (info["client_id"], info["client_secret"])
    return None
