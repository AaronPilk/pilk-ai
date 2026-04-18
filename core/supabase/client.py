"""Supabase client wrapper (foundation — not yet called at runtime).

A thin async HTTP shim over the Supabase REST surface. We deliberately
avoid pulling in the full `supabase-py` SDK until later batches actually
need it — the stdlib + `httpx` already cover the few calls we'll make
(health ping, owner lookup). Keeping the surface small also makes it
easy to mock in tests.

Usage in later batches:

    from core.supabase import SupabaseClient

    sb = SupabaseClient.from_settings(settings)
    if sb.is_configured:
        row = await sb.get_owner_by_email("me@example.com")

For this batch, nothing calls it in production flow. The health route
uses `reachable()` as a sanity ping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from core.config.settings import Settings


@dataclass(frozen=True)
class SupabaseClient:
    """Immutable client config + helpers. Cheap to construct."""

    url: str | None
    anon_key: str | None
    service_role_key: str | None

    @classmethod
    def from_settings(cls, settings: Settings) -> SupabaseClient:
        return cls(
            url=settings.supabase_url,
            anon_key=settings.supabase_anon_key,
            service_role_key=settings.supabase_service_role_key,
        )

    @property
    def is_configured(self) -> bool:
        # Minimum viable config: URL + anon key. Service role is
        # separate — some environments deliberately omit it (CI) and
        # the client still works for anon-level reads.
        return bool(self.url) and bool(self.anon_key)

    @property
    def rest_url(self) -> str | None:
        if not self.url:
            return None
        return f"{self.url.rstrip('/')}/rest/v1"

    def auth_headers(self, *, service_role: bool = False) -> dict[str, str]:
        """Return headers for a REST call.

        service_role=True picks the service key (server-only, bypasses
        RLS). Use it for migrations, admin backfills, and the health
        ping. Default anon-key requests respect RLS.
        """
        key = self.service_role_key if service_role else self.anon_key
        if not self.url or not key:
            return {}
        return {
            "apikey": key,
            "Authorization": f"Bearer {key}",
        }

    async def reachable(self, *, timeout_s: float = 3.0) -> bool:
        """Hit the REST root as a cheap liveness check.

        Returns False on any error — network, auth, project paused.
        Callers should treat this as a yes/no signal, not a diagnostic.
        """
        if not self.is_configured or not self.rest_url:
            return False
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as http:
                r = await http.get(
                    self.rest_url + "/",
                    headers=self.auth_headers(),
                )
            # Supabase's REST root returns 200 with the anon key and
            # 401/403 without. Either response proves the project is
            # up; a 5xx or a connection error does not.
            return r.status_code < 500
        except (httpx.HTTPError, OSError):
            return False

    def public_status(self) -> dict[str, Any]:
        """A safe-to-serialize view for the health endpoint."""
        return {
            "configured": self.is_configured,
            "has_service_role": bool(self.service_role_key),
            "url_host": (
                httpx.URL(self.url).host if self.url else None
            ),
        }


__all__ = ["SupabaseClient"]
