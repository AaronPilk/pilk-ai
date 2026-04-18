"""Role-aware Google account resolution.

PILK treats the *system* identity (sentientpilkai@gmail.com — the
account PILK uses as itself for reports, API signups, verification
mail) as a different thing from the *user* identity (your real inbox
where real people write to you). The two roles live in separate files
with separate scopes, and their tools carry different risk postures.

Nothing here does OAuth; it just points at files. The OAuth dance is
still owned by `scripts.link_google`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from core.logging import get_logger

log = get_logger("pilkd.google.accounts")

GoogleRole = Literal["system", "user"]
ROLES: tuple[GoogleRole, ...] = ("system", "user")

ROLE_LABELS: dict[GoogleRole, str] = {
    "system": "PILK operational mail",
    "user": "Your working mail",
}


def role_dir(home: Path) -> Path:
    """Directory that holds `system.json` and `user.json`."""
    return home / "identity" / "integrations" / "google"


def credentials_path(home: Path, role: GoogleRole) -> Path:
    return role_dir(home) / f"{role}.json"


def legacy_path(home: Path) -> Path:
    """Pre-Batch-K single-account file location."""
    return home / "identity" / "integrations" / "google.json"


def migrate_legacy_if_needed(home: Path) -> Path | None:
    """Move the pre-role file to system.json once, idempotently.

    The original linked account is PILK's own Gmail (sentientpilkai@...),
    which is the *system* role. Users who want to link a second, working
    identity can do so with `--role user` without going through OAuth
    again.

    Returns the new path if a migration happened, else None.
    """
    legacy = legacy_path(home)
    if not legacy.exists():
        return None
    target = credentials_path(home, "system")
    if target.exists():
        # Both files exist — leave them alone. A human needs to decide
        # which to keep; we don't silently overwrite.
        log.warning(
            "google_legacy_kept",
            legacy=str(legacy),
            target=str(target),
            detail="legacy google.json left in place; system.json already present",
        )
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    legacy.rename(target)
    log.info(
        "google_legacy_migrated",
        from_=str(legacy),
        to=str(target),
        role="system",
    )
    return target
