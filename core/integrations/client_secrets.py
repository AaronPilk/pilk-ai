"""Provider → (client_id, client_secret) resolver used by the OAuth flow.

Keeps filesystem reads and env lookups out of the generic flow module.
Returns None when no client is configured for a given provider; the
caller surfaces a clear error.

Lookup order for Slack / LinkedIn / X / Meta:
  1. ``integration_secrets`` SQLite store (values pasted in Settings →
     API Keys) — so the operator can connect without shell env vars.
  2. ``PILK_<PROVIDER>_CLIENT_ID`` / ``PILK_<PROVIDER>_CLIENT_SECRET``
     env vars — kept as a fallback for Railway / local dev.

Google still resolves through its Desktop-client JSON file because
that's the shape Google Cloud Console exports; the env-var fallback
for Google is identical to the others.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from core.secrets import resolve_secret


def load_client(provider: str, *, settings) -> tuple[str, str] | None:
    """Return (client_id, client_secret) for `provider`, or None."""
    if provider == "google":
        return _load_google_client(settings.google_client_secret_path)
    if provider == "slack":
        return _load_pair(
            "slack_client_id", "PILK_SLACK_CLIENT_ID",
            "slack_client_secret", "PILK_SLACK_CLIENT_SECRET",
        )
    if provider == "linkedin":
        return _load_pair(
            "linkedin_client_id", "PILK_LINKEDIN_CLIENT_ID",
            "linkedin_client_secret", "PILK_LINKEDIN_CLIENT_SECRET",
        )
    if provider == "x":
        return _load_pair(
            "x_client_id", "PILK_X_CLIENT_ID",
            "x_client_secret", "PILK_X_CLIENT_SECRET",
        )
    if provider == "meta":
        return _load_pair(
            "meta_client_id", "PILK_META_CLIENT_ID",
            "meta_client_secret", "PILK_META_CLIENT_SECRET",
        )
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
        return (
            "Paste the Slack app's client ID + client secret in "
            "Settings → API Keys (fields 'slack_client_id' + "
            "'slack_client_secret'), or set PILK_SLACK_CLIENT_ID + "
            "PILK_SLACK_CLIENT_SECRET."
        )
    if provider == "linkedin":
        return (
            "Paste the LinkedIn app's client ID + client secret in "
            "Settings → API Keys (fields 'linkedin_client_id' + "
            "'linkedin_client_secret'), or set PILK_LINKEDIN_CLIENT_ID "
            "+ PILK_LINKEDIN_CLIENT_SECRET."
        )
    if provider == "x":
        return (
            "Paste the X developer app's client ID + client secret in "
            "Settings → API Keys (fields 'x_client_id' + "
            "'x_client_secret'), or set PILK_X_CLIENT_ID + "
            "PILK_X_CLIENT_SECRET."
        )
    if provider == "meta":
        return (
            "Paste the Meta app's app ID + app secret in Settings → "
            "API Keys (fields 'meta_client_id' + 'meta_client_secret'), "
            "or set PILK_META_CLIENT_ID + PILK_META_CLIENT_SECRET."
        )
    return None


def _load_pair(
    id_secret_name: str,
    id_env_var: str,
    secret_secret_name: str,
    secret_env_var: str,
) -> tuple[str, str] | None:
    """Resolve a (client_id, client_secret) pair.

    Each half goes through ``resolve_secret``, which checks the
    integration_secrets SQLite store first (Settings → API Keys
    overrides) and falls back to the env var if the store has no row.
    Returns None unless both halves resolve to non-empty values.
    """
    cid = resolve_secret(id_secret_name, env_fallback=os.getenv(id_env_var))
    csec = resolve_secret(secret_secret_name, env_fallback=os.getenv(secret_env_var))
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
