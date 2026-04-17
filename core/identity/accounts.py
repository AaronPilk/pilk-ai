"""Provider-agnostic store for OAuth-connected accounts.

Every connected account — PILK operational Gmail, your working Gmail,
a future Slack workspace — is a `ConnectedAccount` record in
`index.json`, plus a matching OAuth blob in `secrets/{account_id}.json`.

Callers:
- OAuth layer writes records via `add`/`remove`.
- Tool handlers resolve `(provider, role[, account_id])` bindings to
  a live account via `resolve_binding`, then ask for tokens via
  `load_tokens`.
- REST layer reads `list`/`get`/`default` for the Settings UI.

Nothing in here is specific to Google. Provider-specific concerns
(auth URLs, scope catalogs, profile fetchers) live next door in
`core.integrations.provider`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from core.logging import get_logger

log = get_logger("pilkd.identity.accounts")

Role = Literal["system", "user"]


# ── value objects ─────────────────────────────────────────────────────


@dataclass
class AccountBinding:
    """How a tool says which account to use at call time.

    If `account_id` is None, the AccountsStore resolves to the default
    account for `(provider, role)`. The tool never hard-codes a path.
    """

    provider: str
    role: Role
    account_id: str | None = None


@dataclass
class ConnectedAccount:
    account_id: str
    provider: str
    role: Role
    label: str
    email: str | None
    username: str | None
    scopes: list[str]
    status: Literal["connected", "expired", "revoked", "pending"]
    linked_at: str
    last_refreshed_at: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OAuthTokens:
    """The raw OAuth material — never leaves the store unsanitized."""

    access_token: str | None
    refresh_token: str
    client_id: str
    client_secret: str
    scopes: list[str]
    token_uri: str = "https://oauth2.googleapis.com/token"
    extra: dict[str, Any] = field(default_factory=dict)


# ── helpers ───────────────────────────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(s: str) -> str:
    cleaned = _SLUG_RE.sub("-", (s or "").lower()).strip("-")
    return cleaned or "unknown"


def account_id_for(provider: str, role: Role, identifier: str) -> str:
    """Stable, URL-safe account id: `{provider}-{role}-{slug(identifier)}`."""
    return f"{provider}-{role}-{_slug(identifier)}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ── store ─────────────────────────────────────────────────────────────


class AccountsStore:
    """Thread-safe, file-backed registry of connected accounts.

    One JSON index + one secrets file per account. Writes are serialized
    through a single in-process lock; that's enough since pilkd is a
    single-process daemon.
    """

    def __init__(self, home: Path) -> None:
        self._home = home
        self._root = home / "identity" / "accounts"
        self._index_path = self._root / "index.json"
        self._secrets_dir = self._root / "secrets"
        self._lock = Lock()

    # ── lifecycle ─────────────────────────────────────────────────

    def ensure_layout(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._secrets_dir.mkdir(parents=True, exist_ok=True)
        if not self._index_path.exists():
            self._write_index({"accounts": [], "defaults": {}})

    # ── reads ─────────────────────────────────────────────────────

    def list(
        self,
        *,
        provider: str | None = None,
        role: Role | None = None,
    ) -> list[ConnectedAccount]:
        index = self._read_index()
        out: list[ConnectedAccount] = []
        for raw in index.get("accounts", []):
            if provider and raw.get("provider") != provider:
                continue
            if role and raw.get("role") != role:
                continue
            out.append(_account_from_dict(raw))
        return out

    def get(self, account_id: str) -> ConnectedAccount | None:
        for raw in self._read_index().get("accounts", []):
            if raw.get("account_id") == account_id:
                return _account_from_dict(raw)
        return None

    def default_id(self, provider: str, role: Role) -> str | None:
        key = f"{provider}:{role}"
        return self._read_index().get("defaults", {}).get(key)

    def default(self, provider: str, role: Role) -> ConnectedAccount | None:
        aid = self.default_id(provider, role)
        return self.get(aid) if aid else None

    def resolve_binding(self, binding: AccountBinding) -> ConnectedAccount | None:
        if binding.account_id:
            got = self.get(binding.account_id)
            if got and got.provider == binding.provider and got.role == binding.role:
                return got
            return None
        return self.default(binding.provider, binding.role)

    def load_tokens(self, account_id: str) -> OAuthTokens | None:
        path = self._secrets_path(account_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            log.warning("oauth_secret_unreadable", account_id=account_id, error=str(e))
            return None
        return OAuthTokens(
            access_token=data.get("access_token"),
            refresh_token=data.get("refresh_token", ""),
            client_id=data.get("client_id", ""),
            client_secret=data.get("client_secret", ""),
            scopes=list(data.get("scopes") or []),
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            extra={k: v for k, v in data.items() if k not in _TOKEN_FIELDS},
        )

    # ── writes ────────────────────────────────────────────────────

    def upsert(
        self,
        *,
        provider: str,
        role: Role,
        label: str,
        email: str | None,
        username: str | None,
        scopes: list[str],
        tokens: OAuthTokens,
        account_id: str | None = None,
        make_default: bool = False,
    ) -> ConnectedAccount:
        """Add a new account or update an existing one (same account_id)."""
        identifier = email or username or account_id or ""
        aid = account_id or account_id_for(provider, role, identifier)
        account = ConnectedAccount(
            account_id=aid,
            provider=provider,
            role=role,
            label=label,
            email=email,
            username=username,
            scopes=list(scopes),
            status="connected",
            linked_at=_now(),
            last_refreshed_at=_now(),
        )
        with self._lock:
            self.ensure_layout()
            index = self._read_index()
            accounts = [a for a in index.get("accounts", []) if a.get("account_id") != aid]
            accounts.append(asdict(account))
            index["accounts"] = accounts
            defaults = index.setdefault("defaults", {})
            key = f"{provider}:{role}"
            if make_default or key not in defaults:
                defaults[key] = aid
            self._write_index(index)
            self._write_secret(aid, tokens)
        log.info(
            "account_linked",
            provider=provider,
            role=role,
            account_id=aid,
            email=email,
            username=username,
        )
        return account

    def remove(self, account_id: str) -> bool:
        with self._lock:
            index = self._read_index()
            before = len(index.get("accounts", []))
            index["accounts"] = [
                a for a in index.get("accounts", []) if a.get("account_id") != account_id
            ]
            removed = len(index["accounts"]) < before
            if not removed:
                return False
            # If we just removed a default, clear it; the UI will ask the
            # user to pick a new default among any remaining accounts.
            defaults = index.get("defaults", {})
            for key, aid in list(defaults.items()):
                if aid == account_id:
                    defaults.pop(key, None)
            self._write_index(index)
            path = self._secrets_path(account_id)
            if path.exists():
                with contextlib.suppress(Exception):
                    path.unlink()
        log.info("account_removed", account_id=account_id)
        return True

    def set_default(self, account_id: str) -> bool:
        with self._lock:
            index = self._read_index()
            match = next(
                (a for a in index.get("accounts", []) if a.get("account_id") == account_id),
                None,
            )
            if match is None:
                return False
            defaults = index.setdefault("defaults", {})
            defaults[f"{match['provider']}:{match['role']}"] = account_id
            self._write_index(index)
        log.info("account_default_set", account_id=account_id)
        return True

    def update_status(
        self, account_id: str, status: str, *, refreshed: bool = False
    ) -> None:
        with self._lock:
            index = self._read_index()
            for a in index.get("accounts", []):
                if a.get("account_id") == account_id:
                    a["status"] = status
                    if refreshed:
                        a["last_refreshed_at"] = _now()
                    break
            self._write_index(index)

    # ── internals ─────────────────────────────────────────────────

    def _read_index(self) -> dict:
        if not self._index_path.exists():
            return {"accounts": [], "defaults": {}}
        try:
            return json.loads(self._index_path.read_text())
        except Exception as e:
            log.warning("accounts_index_unreadable", error=str(e))
            return {"accounts": [], "defaults": {}}

    def _write_index(self, data: dict) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        tmp = self._index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, self._index_path)

    def _secrets_path(self, account_id: str) -> Path:
        return self._secrets_dir / f"{account_id}.json"

    def _write_secret(self, account_id: str, tokens: OAuthTokens) -> None:
        self._secrets_dir.mkdir(parents=True, exist_ok=True)
        blob = {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "client_id": tokens.client_id,
            "client_secret": tokens.client_secret,
            "scopes": list(tokens.scopes),
            "token_uri": tokens.token_uri,
            **(tokens.extra or {}),
        }
        path = self._secrets_path(account_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(blob, indent=2))
        os.replace(tmp, path)
        with contextlib.suppress(Exception):
            path.chmod(0o600)


_TOKEN_FIELDS = frozenset(
    {"access_token", "refresh_token", "client_id", "client_secret", "scopes", "token_uri"}
)


def _account_from_dict(raw: dict) -> ConnectedAccount:
    return ConnectedAccount(
        account_id=raw["account_id"],
        provider=raw["provider"],
        role=raw["role"],
        label=raw.get("label", ""),
        email=raw.get("email"),
        username=raw.get("username"),
        scopes=list(raw.get("scopes") or []),
        status=raw.get("status", "connected"),
        linked_at=raw.get("linked_at", ""),
        last_refreshed_at=raw.get("last_refreshed_at"),
    )
