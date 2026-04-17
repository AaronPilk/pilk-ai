"""One-time migration from Batch K's per-role Google files into AccountsStore.

Reads `identity/integrations/google/{system,user}.json` (if present) and
upserts matching records into the new `identity/accounts/` store. The
originals are renamed with a `.migrated` suffix rather than deleted so
the user can verify and remove them by hand.

Idempotent — re-running is a no-op once the renames are in place.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from core.identity import AccountsStore, account_id_for
from core.identity.accounts import OAuthTokens
from core.logging import get_logger

log = get_logger("pilkd.oauth.migration")


def migrate_batch_k_google_files(home: Path, store: AccountsStore) -> list[str]:
    """Returns a list of account_ids that were imported (empty on no-op)."""
    base = home / "identity" / "integrations" / "google"
    if not base.exists():
        return []
    imported: list[str] = []
    for role in ("system", "user"):
        src = base / f"{role}.json"
        if not src.exists():
            continue
        try:
            data = json.loads(src.read_text())
        except Exception as e:
            log.warning("legacy_google_unreadable", role=role, error=str(e))
            continue
        refresh = data.get("refresh_token")
        if not refresh:
            log.warning("legacy_google_no_refresh", role=role)
            continue
        email = data.get("email")
        identifier = email or role
        aid = account_id_for("google", role, identifier)  # type: ignore[arg-type]
        if store.get(aid) is not None:
            # Already imported on a previous boot; just rename the source
            # if the user hadn't finished cleaning up.
            _rename_as_migrated(src)
            continue
        tokens = OAuthTokens(
            access_token=data.get("access_token"),
            refresh_token=refresh,
            client_id=data.get("client_id", ""),
            client_secret=data.get("client_secret", ""),
            scopes=list(data.get("scopes") or []),
        )
        store.upsert(
            provider="google",
            role=role,  # type: ignore[arg-type]
            label=_legacy_label(role, email),
            email=email,
            username=None,
            scopes=tokens.scopes,
            tokens=tokens,
            account_id=aid,
            make_default=True,  # single account per role in the legacy layout
        )
        imported.append(aid)
        _rename_as_migrated(src)
        log.info("legacy_google_migrated", role=role, account_id=aid, email=email)
    return imported


def _legacy_label(role: str, email: str | None) -> str:
    who = email or ("PILK operational" if role == "system" else "Your working mail")
    return f"Google · {'PILK' if role == 'system' else 'You'} · {who}"


def _rename_as_migrated(path: Path) -> None:
    target = path.with_suffix(path.suffix + ".migrated")
    if target.exists():
        return
    with contextlib.suppress(Exception):
        path.rename(target)
