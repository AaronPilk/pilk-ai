"""Go High Level HTTP client.

Thin httpx wrapper around the LeadConnector REST API
(``https://services.leadconnectorhq.com``). Every method is one
round-trip; no caching, no retry. The client itself is stateless
beyond auth config so a runtime secret rotation (paste a new
``ghl_api_key`` into Settings → API Keys) lands on the next
invocation without a daemon restart — tool handlers build a fresh
client per call via :func:`client_from_settings`.

### API version

GHL's API requires a ``Version`` header on every call. We pin to
``2021-07-28`` — the latest stable at the time of writing. When GHL
issues a new version we'll bump the constant and re-run the test
suite; the JSON shapes don't change in minor revisions.

### Auth

Bearer agency PIT. Every request carries
``Authorization: Bearer <ghl_api_key>``. No token refresh path —
the PIT doesn't expire until explicitly revoked in GHL's UI.

### Error handling

One exception type — :class:`GHLError` — raised on any non-2xx
response. Wraps HTTP status + the ``message`` (or ``error``) field
from GHL's JSON body. Tool handlers turn that into a
:class:`ToolOutcome` with actionable copy for common statuses
(401 = bad token, 403 = token missing a scope, 404 = wrong id or
location, 422 = validation).

### Location scoping

GHL calls are per-sub-account. :func:`resolve_location_id` picks the
effective location id (arg override → settings default → raise). The
helper is isolated so every tool behaves identically around
location resolution.
"""

from __future__ import annotations

from typing import Any

import httpx

from core.logging import get_logger

log = get_logger("pilkd.ghl")

GHL_API_BASE = "https://services.leadconnectorhq.com"
GHL_API_VERSION = "2021-07-28"
DEFAULT_TIMEOUT_S = 30.0


class GHLError(Exception):
    """Raised on non-2xx from the GHL API. ``message`` is the
    server-side human-readable string; ``raw`` is the decoded JSON
    body so callers can inspect structured fields if needed."""

    def __init__(
        self, status: int, message: str, raw: Any = None,
    ) -> None:
        super().__init__(f"GHL {status}: {message}")
        self.status = status
        self.message = message
        self.raw = raw


class GHLNotConfiguredError(RuntimeError):
    """Raised by :func:`client_from_settings` when ``ghl_api_key``
    isn't set. Tool handlers catch + surface a friendly
    "add the key in Settings → API Keys" message instead of
    crashing."""


def resolve_location_id(
    *,
    arg: str | None,
    default: str | None,
) -> str:
    """Pick the effective location id for a tool call.

    Order: explicit ``arg`` → settings default → raise. Raising
    forces the caller to handle the "no location available" case
    explicitly rather than silently writing to the wrong account.
    """
    if arg:
        return arg.strip()
    if default:
        return default.strip()
    raise GHLError(
        status=400,
        message=(
            "no location_id: pass one to the tool or set "
            "ghl_default_location_id in Settings → API Keys."
        ),
    )


class GHLClient:
    """Per-call async HTTP client for the GHL / LeadConnector API.

    Constructed fresh on each tool invocation (cheap) so runtime
    secret rotation is picked up without a daemon restart. Methods
    are named after GHL's REST endpoints: ``contacts_create``,
    ``contacts_search``, etc.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str = GHL_API_BASE,
        api_version: str = GHL_API_VERSION,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._base = api_base.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Version": api_version,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    # ── low-level HTTP helpers ────────────────────────────────

    async def _get(
        self, path: str, *, params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(
                f"{self._base}{path}",
                headers=self._headers,
                params=params,
            )
        return _decode(r, f"GET {path}")

    async def _post(
        self, path: str, *, json: dict[str, Any],
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(
                f"{self._base}{path}",
                headers=self._headers,
                json=json,
            )
        return _decode(r, f"POST {path}")

    async def _put(
        self, path: str, *, json: dict[str, Any],
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.put(
                f"{self._base}{path}",
                headers=self._headers,
                json=json,
            )
        return _decode(r, f"PUT {path}")

    async def _delete(self, path: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.delete(
                f"{self._base}{path}",
                headers=self._headers,
            )
        return _decode(r, f"DELETE {path}")

    # ── contacts ──────────────────────────────────────────────

    async def contacts_create(
        self, *, location_id: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        body = {"locationId": location_id, **payload}
        return await self._post("/contacts/", json=body)

    async def contacts_get(self, contact_id: str) -> dict[str, Any]:
        return await self._get(f"/contacts/{contact_id}")

    async def contacts_search(
        self,
        *,
        location_id: str,
        query: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Search-by-filter over a location's contacts.

        GHL exposes two surfaces: a simple ``/contacts/lookup`` for
        exact email/phone, and a richer ``/contacts/search`` for
        query strings. We route between them by which arg was
        provided. Caller picks one: email / phone / query (email
        wins if multiple are set — exact match beats substring).
        """
        if email:
            return await self._get(
                "/contacts/",
                params={
                    "locationId": location_id,
                    "email": email,
                    "limit": int(limit),
                },
            )
        if phone:
            return await self._get(
                "/contacts/",
                params={
                    "locationId": location_id,
                    "phone": phone,
                    "limit": int(limit),
                },
            )
        return await self._get(
            "/contacts/",
            params={
                "locationId": location_id,
                "query": query or "",
                "limit": int(limit),
            },
        )

    async def contacts_update(
        self, contact_id: str, *, payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._put(
            f"/contacts/{contact_id}", json=payload,
        )

    async def contacts_delete(self, contact_id: str) -> dict[str, Any]:
        return await self._delete(f"/contacts/{contact_id}")

    async def contacts_add_tags(
        self, contact_id: str, *, tags: list[str],
    ) -> dict[str, Any]:
        return await self._post(
            f"/contacts/{contact_id}/tags",
            json={"tags": tags},
        )

    async def contacts_remove_tags(
        self, contact_id: str, *, tags: list[str],
    ) -> dict[str, Any]:
        # GHL exposes tag removal on the same subpath via DELETE
        # with a JSON body. httpx's .delete() doesn't take a JSON
        # body by default; we use .request() to keep the shape.
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.request(
                "DELETE",
                f"{self._base}/contacts/{contact_id}/tags",
                headers=self._headers,
                json={"tags": tags},
            )
        return _decode(r, f"DELETE /contacts/{contact_id}/tags")

    async def contacts_add_note(
        self,
        contact_id: str,
        *,
        body: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"body": body}
        if user_id:
            payload["userId"] = user_id
        return await self._post(
            f"/contacts/{contact_id}/notes", json=payload,
        )

    # ── opportunities + pipelines ─────────────────────────────
    #
    # Pipelines are the sales-stage configuration; opportunities
    # are individual deals moving through those stages. GHL's API
    # exposes pipelines as read-only from the integration side
    # (operators configure them in the UI) and opportunities as
    # full CRUD. Moving an opportunity between stages is just a
    # PUT with a new ``pipelineStageId``, but we expose it as a
    # dedicated method/tool since "advance the deal" is the most
    # common operation and keeping it distinct makes the planner's
    # tool selection cleaner.

    async def pipelines_list(
        self, *, location_id: str,
    ) -> dict[str, Any]:
        return await self._get(
            "/opportunities/pipelines",
            params={"locationId": location_id},
        )

    async def opportunities_create(
        self, *, location_id: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        body = {"locationId": location_id, **payload}
        return await self._post("/opportunities/", json=body)

    async def opportunities_get(
        self, opportunity_id: str,
    ) -> dict[str, Any]:
        return await self._get(
            f"/opportunities/{opportunity_id}"
        )

    async def opportunities_search(
        self,
        *,
        location_id: str,
        query: str | None = None,
        pipeline_id: str | None = None,
        pipeline_stage_id: str | None = None,
        status: str | None = None,
        assigned_to: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Rich filter over a location's opportunities.

        Every filter is optional; unset ones are omitted from the
        query string so GHL's endpoint sees only the constraints
        the caller actually asked for.
        """
        params: dict[str, Any] = {
            "location_id": location_id,
            "limit": int(limit),
        }
        if query:
            params["query"] = query
        if pipeline_id:
            params["pipeline_id"] = pipeline_id
        if pipeline_stage_id:
            params["pipeline_stage_id"] = pipeline_stage_id
        if status:
            params["status"] = status
        if assigned_to:
            params["assigned_to"] = assigned_to
        return await self._get("/opportunities/search", params=params)

    async def opportunities_update(
        self,
        opportunity_id: str,
        *,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._put(
            f"/opportunities/{opportunity_id}", json=payload,
        )

    async def opportunities_move_stage(
        self,
        opportunity_id: str,
        *,
        pipeline_stage_id: str,
        pipeline_id: str | None = None,
    ) -> dict[str, Any]:
        """Shortcut over ``opportunities_update`` for the common
        "advance / regress this deal" operation.

        ``pipeline_id`` is technically optional for GHL when the
        stage id is globally unique, but GHL has been known to
        422 when the target stage belongs to a different pipeline
        than the opportunity currently sits in. We pass it through
        when set so the planner can be explicit.
        """
        payload: dict[str, Any] = {"pipelineStageId": pipeline_stage_id}
        if pipeline_id:
            payload["pipelineId"] = pipeline_id
        return await self.opportunities_update(
            opportunity_id, payload=payload,
        )

    async def opportunities_delete(
        self, opportunity_id: str,
    ) -> dict[str, Any]:
        return await self._delete(
            f"/opportunities/{opportunity_id}"
        )

    # ── meta (agency + directory reads) ───────────────────────

    async def locations_list(
        self,
        *,
        company_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List sub-accounts visible to this PIT.

        Agency PITs see every location under the agency; location
        PITs see only their own. ``company_id`` narrows further if
        the token has access to multiple companies (rare).
        """
        params: dict[str, Any] = {"limit": int(limit)}
        if company_id:
            params["companyId"] = company_id
        return await self._get("/locations/search", params=params)

    async def users_list(
        self, *, location_id: str, limit: int = 100,
    ) -> dict[str, Any]:
        return await self._get(
            "/users/",
            params={"locationId": location_id, "limit": int(limit)},
        )

    async def custom_fields_list(
        self, *, location_id: str,
    ) -> dict[str, Any]:
        return await self._get(
            f"/locations/{location_id}/customFields"
        )


# ── decode + factory helpers ──────────────────────────────────


def _decode(resp: httpx.Response, method: str) -> dict[str, Any]:
    """Uniform error decode. GHL returns ``{"message": "...", ...}``
    or ``{"error": "...", ...}`` on failure; hoist either into a
    :class:`GHLError` so tool handlers only catch one type."""
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        raise GHLError(
            status=resp.status_code,
            message=(
                f"{method}: non-JSON response "
                f"({resp.text[:160]!r})"
            ),
        ) from None
    if resp.is_success:
        return body if isinstance(body, dict) else {"result": body}
    message = ""
    if isinstance(body, dict):
        message = (
            body.get("message")
            or body.get("error")
            or ""
        )
        # Some GHL endpoints nest the message under errors[0].message
        errors = body.get("errors")
        if not message and isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                message = first.get("message") or ""
    if not message:
        message = f"HTTP {resp.status_code}"
    raise GHLError(
        status=resp.status_code, message=message, raw=body,
    )


def client_from_settings() -> GHLClient:
    """Build a :class:`GHLClient` from the live secret store.

    Raises :class:`GHLNotConfiguredError` when ``ghl_api_key`` is
    missing — tool handlers catch + surface the "add the key in
    Settings → API Keys" message.
    """
    from core.config import get_settings
    from core.secrets import resolve_secret

    settings = get_settings()
    key = resolve_secret("ghl_api_key", settings.ghl_api_key)
    if not key:
        raise GHLNotConfiguredError(
            "ghl_api_key is not set — add an agency PIT in "
            "Settings → API Keys."
        )
    return GHLClient(api_key=key, api_base=settings.ghl_api_base)


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "GHL_API_BASE",
    "GHL_API_VERSION",
    "GHLClient",
    "GHLError",
    "GHLNotConfiguredError",
    "client_from_settings",
    "resolve_location_id",
]
