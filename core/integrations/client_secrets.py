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
    # Slack, LinkedIn, etc. slot in here as they land. Each reads its
    # own env vars or config file and returns a pair.
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
