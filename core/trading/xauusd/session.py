"""Tracks which Browserbase session (if any) the XAUUSD agent is
attached to.

The attach flow is the *runtime* permission model for live trading:
there's no "am I allowed to trade?" bit stored in config. Instead, the
operator logs into Hugosway manually (in a Browserbase live-view
browser), then explicitly hands the session to the agent via
``xauusd_take_over``. Until that happens, every execution path refuses.

Single-tenant for v1 — there can only be one attached session at a time.
The Phase 2 migration to per-user scope replaces this singleton with a
``user_id``-keyed store; the rest of the code only goes through the
three public helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class AttachedSession:
    session_id: str
    account_type: str  # "demo" | "live"
    attached_at: str
    account_id: str | None = None
    note: str | None = None


_attached: AttachedSession | None = None


class NotAttachedError(Exception):
    """Raised when a broker-bound tool runs with no attached session."""


def set_attached_session(
    *,
    session_id: str,
    account_type: str,
    account_id: str | None = None,
    note: str | None = None,
) -> AttachedSession:
    global _attached
    _attached = AttachedSession(
        session_id=session_id,
        account_type=account_type,
        attached_at=datetime.now(UTC).isoformat(),
        account_id=account_id,
        note=note,
    )
    return _attached


def clear_attached_session() -> AttachedSession | None:
    """Detach. Returns whatever was attached (for journaling)."""
    global _attached
    prev = _attached
    _attached = None
    return prev


def get_attached_session() -> AttachedSession | None:
    return _attached


def require_attached_session() -> AttachedSession:
    if _attached is None:
        raise NotAttachedError(
            "No attached broker session. Call xauusd_take_over first."
        )
    return _attached


__all__ = [
    "AttachedSession",
    "NotAttachedError",
    "clear_attached_session",
    "get_attached_session",
    "require_attached_session",
    "set_attached_session",
]
