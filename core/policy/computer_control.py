"""Guardrails for the IRREVERSIBLE ``computer_*`` tool family.

The ``computer_control`` tools let PILK reach outside the workspace
sandbox — read + write files anywhere under ``$HOME``, run unscoped
shell, drive macOS via AppleScript. These operations are strictly
more dangerous than anything else PILK does, so they ship gated by
five independent safety controls, implemented here:

1. **Enable flag.** The whole surface stays inert unless the
   operator has flipped ``computer_control_enabled`` to ``"true"`` in
   Settings. This is a deliberate hurdle — the operator has to
   actively authorise the scope before any tool can even try to run.

2. **Per-call confirmation token.** Every tool call happens in two
   steps: (a) the agent calls the tool without a ``confirmation_token``;
   the tool replies with a fresh single-use token and a preview of
   what would be executed; (b) the agent re-calls with the token to
   actually execute. Tokens expire after 5 minutes and are tied to
   the exact tool name + args, so they can't be replayed against a
   different payload.

3. **Hard-block paths.** A fixed list of paths is refused regardless
   of enable state or token — ``~/.ssh/``, ``~/.aws/``, ``~/.gnupg/``,
   ``/etc/``, ``/System/``, the macOS keychain. Not configurable.
   These are the paths where a bad call really is unrecoverable.

4. **Daily rate limit.** A shared counter caps total
   IRREVERSIBLE-class ``computer_*`` calls per UTC day. Default 20;
   operator can tighten but not loosen above the hard ceiling of 100.
   Resets at UTC midnight.

5. **Dedicated audit log.** Every verified call lands in
   ``~/PILK/logs/computer-control.jsonl`` with tool + args + outcome
   + timestamp. Append-only JSONL so Sentinel can tail-watch and
   alert on anomalies.

The gate is a singleton — all four tools share one instance so the
rate limit + audit log are consistent across them. Stored in
``app.state.computer_control`` at startup.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from core.logging import get_logger

log = get_logger("pilkd.policy.computer_control")

# Tokens live 5 minutes — long enough for the agent to re-call after
# surfacing the preview, short enough that a leaked token can't be
# replayed half an hour later.
CONFIRMATION_TOKEN_TTL_S = 5 * 60

DAILY_LIMIT_DEFAULT = 20
DAILY_LIMIT_HARD_CEILING = 100  # even a determined operator can't bypass

# Absolute paths (and prefixes) that remain permanently off-limits.
# macOS + Linux default sensitive locations; the list is deliberately
# short because false positives create operator friction. Add more
# via the HARD_BLOCK env var if a specific deployment needs it.
DEFAULT_HARD_BLOCK_PREFIXES: tuple[str, ...] = (
    "/.ssh/",
    "/.aws/",
    "/.gnupg/",
    "/.config/gh/",
    "/.config/gcloud/",
    "/Library/Keychains/",
    "/etc/",
    "/System/",
    "/private/etc/",
    # ``/private/var/`` previously blanket-blocked, but on macOS the
    # per-user TMPDIR lives at ``/private/var/folders/<id>/...`` —
    # blocking that whole subtree breaks every legitimate use of
    # ``tempfile`` (and every pytest fixture using tmp_path). Narrow
    # to the actual sensitive subtrees instead.
    "/private/var/db/",
    "/private/var/log/",
    "/private/var/root/",
)


class ComputerControlDisabledError(RuntimeError):
    """Raised when a tool is called while the whole surface is off."""


class TokenRequiredError(RuntimeError):
    """Raised when a call needs a fresh or valid confirmation token.
    The accompanying token is attached for the tool to surface."""

    def __init__(self, token: str, expires_at: float):
        super().__init__("confirmation_token required")
        self.token = token
        self.expires_at = expires_at


class BlockedPathError(ValueError):
    """Raised when a call targets a hard-blocked path."""


class DailyLimitExceededError(RuntimeError):
    """Raised when the UTC-day counter hits the limit."""


@dataclass
class _PendingToken:
    token: str
    tool: str
    fingerprint: str          # hash-equivalent identifier of args
    issued_at: float          # unix ts
    expires_at: float


@dataclass
class _DailyCounter:
    utc_date: str             # YYYY-MM-DD
    count: int


@dataclass
class AuditEntry:
    ts: str
    tool: str
    args_summary: str
    outcome: str              # "ok" | "error" | "denied"
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "tool": self.tool,
            "args": self.args_summary,
            "outcome": self.outcome,
            "detail": self.detail,
        }


@dataclass
class ComputerControlGate:
    """Shared gate for every computer_* tool. All mutations go
    through the in-memory lock — tokens + daily counter are single-
    process state; multi-tenant comes later if ever."""

    audit_path: Path
    daily_limit: int = DAILY_LIMIT_DEFAULT
    hard_block_prefixes: tuple[str, ...] = DEFAULT_HARD_BLOCK_PREFIXES
    _lock: Lock = field(default_factory=Lock)
    _pending: dict[str, _PendingToken] = field(default_factory=dict)
    _daily: _DailyCounter = field(
        default_factory=lambda: _DailyCounter(
            utc_date=datetime.now(tz=UTC).strftime("%Y-%m-%d"),
            count=0,
        )
    )

    # ── enable state ──────────────────────────────────────────────

    @staticmethod
    def is_enabled() -> bool:
        """Reads the dynamic setting — checked on every call so the
        operator can flip the kill switch without a daemon restart."""
        from core.config import get_settings
        from core.secrets import resolve_secret

        s = get_settings()
        raw = resolve_secret(
            "computer_control_enabled",
            s.computer_control_enabled,
        )
        return bool(raw) and str(raw).strip().lower() in {
            "1", "true", "yes", "on",
        }

    def require_enabled(self) -> None:
        if not self.is_enabled():
            raise ComputerControlDisabledError(
                "Computer control is disabled. To enable, set "
                "computer_control_enabled='true' in Settings → API "
                "Keys. This unlocks IRREVERSIBLE-class tools that "
                "can read + write anywhere under $HOME and run "
                "unscoped shell / AppleScript. Leave off unless you "
                "explicitly want that."
            )

    # ── confirmation tokens ──────────────────────────────────────

    def _fingerprint(self, tool: str, args: dict[str, Any]) -> str:
        """Stable string representation of (tool, args) so a token
        issued for one payload can't be used for a different one."""
        # json.dumps with sort_keys + ensure_ascii gives us a
        # deterministic, safe string; unknown types fall back to repr.
        try:
            payload = json.dumps(
                args, sort_keys=True, default=repr, ensure_ascii=False,
            )
        except TypeError:
            payload = repr(args)
        return f"{tool}::{payload}"

    def issue_token(self, tool: str, args: dict[str, Any]) -> _PendingToken:
        """Mint a single-use token for this specific (tool, args)
        payload. Expires after CONFIRMATION_TOKEN_TTL_S seconds."""
        token = secrets.token_urlsafe(18)
        now = time.time()
        entry = _PendingToken(
            token=token,
            tool=tool,
            fingerprint=self._fingerprint(tool, args),
            issued_at=now,
            expires_at=now + CONFIRMATION_TOKEN_TTL_S,
        )
        with self._lock:
            self._sweep_expired_locked(now)
            self._pending[token] = entry
        return entry

    def verify_and_consume_token(
        self, tool: str, args: dict[str, Any], token: str,
    ) -> None:
        """Raise if the token is invalid / expired / for a different
        payload. On success, the token is consumed (single-use)."""
        now = time.time()
        fingerprint = self._fingerprint(tool, args)
        with self._lock:
            self._sweep_expired_locked(now)
            entry = self._pending.get(token)
            if entry is None:
                raise TokenRequiredError(
                    *self._issue_locked(tool, args, now)
                )
            if entry.tool != tool:
                del self._pending[token]
                raise TokenRequiredError(
                    *self._issue_locked(tool, args, now)
                )
            if entry.fingerprint != fingerprint:
                # The args mutated between propose and execute. That
                # might be an LLM accident; it might be an injection.
                # Either way: reject + require a fresh preview.
                del self._pending[token]
                raise TokenRequiredError(
                    *self._issue_locked(tool, args, now)
                )
            if entry.expires_at < now:
                del self._pending[token]
                raise TokenRequiredError(
                    *self._issue_locked(tool, args, now)
                )
            del self._pending[token]

    def _issue_locked(
        self, tool: str, args: dict[str, Any], now: float,
    ) -> tuple[str, float]:
        """Re-use issue_token logic but assuming the lock is held —
        avoids a recursive acquire."""
        token = secrets.token_urlsafe(18)
        self._pending[token] = _PendingToken(
            token=token,
            tool=tool,
            fingerprint=self._fingerprint(tool, args),
            issued_at=now,
            expires_at=now + CONFIRMATION_TOKEN_TTL_S,
        )
        return token, self._pending[token].expires_at

    def _sweep_expired_locked(self, now: float) -> None:
        for k in list(self._pending):
            if self._pending[k].expires_at < now:
                del self._pending[k]

    # ── daily rate limit ─────────────────────────────────────────

    def check_and_bump_daily(self) -> int:
        """Increment + check. Raises DailyLimitExceededError when the cap
        is hit. Returns the post-increment count so the tool can
        surface how many calls remain."""
        today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        with self._lock:
            if self._daily.utc_date != today:
                self._daily = _DailyCounter(utc_date=today, count=0)
            if self._daily.count >= self.daily_limit:
                raise DailyLimitExceededError(
                    f"Daily IRREVERSIBLE limit hit "
                    f"({self._daily.count}/{self.daily_limit} today). "
                    "Resets at UTC midnight. To run anyway, raise "
                    "computer_control_daily_limit in Settings — but "
                    "the hard ceiling is "
                    f"{DAILY_LIMIT_HARD_CEILING}."
                )
            self._daily.count += 1
            return self._daily.count

    # ── hard-block paths ─────────────────────────────────────────

    def check_path(self, path: Path) -> None:
        """Raise BlockedPathError if the resolved path falls inside any
        hard-block prefix. Callers should resolve() first to foil
        symlink escapes."""
        resolved = path.expanduser().resolve()
        s = str(resolved)
        for prefix in self.hard_block_prefixes:
            # Prefix is "/.ssh/"; we match against both an absolute
            # and home-relative interpretation.
            home = str(Path.home())
            if s.startswith(home + prefix):
                raise BlockedPathError(
                    f"{resolved} is under a hard-blocked prefix "
                    f"({prefix}). Not configurable. Move the target "
                    "or use a platform-specific tool instead."
                )
            if s.startswith(prefix):
                raise BlockedPathError(
                    f"{resolved} is under a hard-blocked prefix "
                    f"({prefix}). Not configurable."
                )
        return None

    # ── audit log ────────────────────────────────────────────────

    def append_audit(self, entry: AuditEntry) -> None:
        """Append one line to the JSONL audit log. Failures are
        swallowed — the tool call already succeeded; a failed audit
        write should not tell the agent it failed. But we log the
        error so sentinel can tell someone's filesystem is broken."""
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.as_dict()) + "\n")
        except OSError as e:
            log.warning(
                "computer_control_audit_write_failed",
                path=str(self.audit_path),
                error=str(e),
            )


def build_default_gate(pilk_home: Path) -> ComputerControlGate:
    """Factory wired at app startup. Reads the daily-limit setting
    and clamps it to the hard ceiling."""
    from core.config import get_settings

    s = get_settings()
    limit = max(1, min(s.computer_control_daily_limit, DAILY_LIMIT_HARD_CEILING))
    audit_path = pilk_home.expanduser() / "logs" / "computer-control.jsonl"
    with contextlib.suppress(OSError):
        os.makedirs(audit_path.parent, exist_ok=True)
    return ComputerControlGate(
        audit_path=audit_path,
        daily_limit=limit,
    )


def _iso_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def fresh_audit_entry(
    tool: str, args_summary: str, outcome: str, detail: str | None = None,
) -> AuditEntry:
    return AuditEntry(
        ts=_iso_now(),
        tool=tool,
        args_summary=args_summary,
        outcome=outcome,
        detail=detail,
    )


__all__ = [
    "CONFIRMATION_TOKEN_TTL_S",
    "DAILY_LIMIT_DEFAULT",
    "DAILY_LIMIT_HARD_CEILING",
    "AuditEntry",
    "BlockedPathError",
    "ComputerControlDisabledError",
    "ComputerControlGate",
    "DailyLimitExceededError",
    "TokenRequiredError",
    "build_default_gate",
    "fresh_audit_entry",
]
