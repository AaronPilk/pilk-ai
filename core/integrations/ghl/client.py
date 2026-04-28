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

    # ── conversations (SMS + email + inbound history) ─────────
    #
    # GHL unifies SMS, email, WhatsApp, Facebook Messenger, Instagram
    # DMs, and Google Business chat under one "conversations" surface.
    # We expose two channel-specific senders (SMS, email) because they
    # cover the operator's day-to-day and the channel rules differ
    # enough that a single ``send(type=...)`` would hide real differences
    # (email needs subject + HTML; SMS doesn't). The other channels can
    # ride the generic send path via ``_send_conversation_message`` when
    # a tool lands for them.

    async def _send_conversation_message(
        self,
        *,
        message_type: str,
        contact_id: str,
        location_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Single POST that every channel funnels through.
        Shared by ``send_sms`` + ``send_email`` (and future
        ``send_whatsapp`` / ``send_ig_dm`` etc.)."""
        payload: dict[str, Any] = {
            "type": message_type,
            "contactId": contact_id,
            "locationId": location_id,
            **body,
        }
        return await self._post("/conversations/messages", json=payload)

    async def conversations_send_sms(
        self,
        *,
        contact_id: str,
        location_id: str,
        message: str,
        from_number: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"message": message}
        if from_number:
            body["fromNumber"] = from_number
        return await self._send_conversation_message(
            message_type="SMS",
            contact_id=contact_id,
            location_id=location_id,
            body=body,
        )

    async def conversations_send_email(
        self,
        *,
        contact_id: str,
        location_id: str,
        subject: str,
        html: str | None = None,
        text: str | None = None,
        from_email: str | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        """Send an email to a contact through GHL's email channel.

        Either ``html`` or ``text`` must be set — GHL accepts one or
        both. Passing both is fine and lets GHL pick per-recipient
        preferences.
        """
        body: dict[str, Any] = {"subject": subject}
        if html:
            body["html"] = html
        if text:
            body["message"] = text
        if from_email:
            body["emailFrom"] = from_email
        if reply_to:
            body["replyTo"] = reply_to
        return await self._send_conversation_message(
            message_type="Email",
            contact_id=contact_id,
            location_id=location_id,
            body=body,
        )

    async def conversations_search(
        self,
        *,
        location_id: str,
        contact_id: str | None = None,
        last_message_type: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Search conversation threads in a location.

        ``contact_id`` narrows to one contact's threads — the common
        path when the planner already has a contact and wants
        "what's the conversation history with them?".
        ``last_message_type`` filters by the last message's channel
        (SMS / Email / …) so the planner can answer "any unread
        SMS?" without paging through email-only threads.
        """
        params: dict[str, Any] = {
            "locationId": location_id,
            "limit": int(limit),
        }
        if contact_id:
            params["contactId"] = contact_id
        if last_message_type:
            params["lastMessageType"] = last_message_type
        return await self._get(
            "/conversations/search", params=params,
        )

    async def conversations_get_messages(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await self._get(
            f"/conversations/{conversation_id}/messages",
            params={"limit": int(limit)},
        )

    # ── calendars + appointments ──────────────────────────────
    #
    # Calendars in GHL are configurable booking pages (a single
    # user / a team / a round-robin). Appointments are the booked
    # slots on those calendars. We expose the common CRUD +
    # availability lookup; more exotic operations (custom block-off,
    # holidays) stay in the GHL UI.

    async def calendars_list(
        self, *, location_id: str,
    ) -> dict[str, Any]:
        return await self._get(
            "/calendars/",
            params={"locationId": location_id},
        )

    async def calendars_free_slots(
        self,
        calendar_id: str,
        *,
        start_date_ms: int,
        end_date_ms: int,
        timezone: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Fetch available booking slots for a calendar.

        ``start_date_ms`` / ``end_date_ms`` are epoch milliseconds
        (GHL's chosen unit). The tool-level wrapper handles the ISO
        date → ms conversion so the planner never has to.
        """
        params: dict[str, Any] = {
            "startDate": int(start_date_ms),
            "endDate": int(end_date_ms),
        }
        if timezone:
            params["timezone"] = timezone
        if user_id:
            params["userId"] = user_id
        return await self._get(
            f"/calendars/{calendar_id}/free-slots",
            params=params,
        )

    async def appointments_list(
        self,
        *,
        location_id: str,
        calendar_id: str | None = None,
        contact_id: str | None = None,
        user_id: str | None = None,
        start_date_ms: int | None = None,
        end_date_ms: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"locationId": location_id}
        if calendar_id:
            params["calendarId"] = calendar_id
        if contact_id:
            params["contactId"] = contact_id
        if user_id:
            params["userId"] = user_id
        if start_date_ms is not None:
            params["startDate"] = int(start_date_ms)
        if end_date_ms is not None:
            params["endDate"] = int(end_date_ms)
        return await self._get("/calendars/events", params=params)

    async def appointments_create(
        self,
        *,
        calendar_id: str,
        contact_id: str,
        location_id: str,
        start_time_iso: str,
        end_time_iso: str | None = None,
        title: str | None = None,
        appointment_status: str | None = None,
        assigned_user_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "calendarId": calendar_id,
            "contactId": contact_id,
            "locationId": location_id,
            "startTime": start_time_iso,
        }
        if end_time_iso:
            payload["endTime"] = end_time_iso
        if title:
            payload["title"] = title
        if appointment_status:
            payload["appointmentStatus"] = appointment_status
        if assigned_user_id:
            payload["assignedUserId"] = assigned_user_id
        return await self._post(
            "/calendars/events/appointments", json=payload,
        )

    async def appointments_update(
        self,
        appointment_id: str,
        *,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._put(
            f"/calendars/events/appointments/{appointment_id}",
            json=payload,
        )

    # ── tasks ─────────────────────────────────────────────────

    async def tasks_list(
        self, contact_id: str,
    ) -> dict[str, Any]:
        return await self._get(f"/contacts/{contact_id}/tasks")

    async def tasks_create(
        self,
        contact_id: str,
        *,
        title: str,
        body: str | None = None,
        due_date_iso: str | None = None,
        assigned_to: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title}
        if body:
            payload["body"] = body
        if due_date_iso:
            payload["dueDate"] = due_date_iso
        if assigned_to:
            payload["assignedTo"] = assigned_to
        return await self._post(
            f"/contacts/{contact_id}/tasks", json=payload,
        )

    # ── workflows ─────────────────────────────────────────────

    async def workflows_list(
        self, *, location_id: str,
    ) -> dict[str, Any]:
        return await self._get(
            "/workflows/",
            params={"locationId": location_id},
        )

    async def workflows_add_contact(
        self,
        *,
        contact_id: str,
        workflow_id: str,
        event_start_time_iso: str | None = None,
    ) -> dict[str, Any]:
        """Enroll a contact in a workflow.

        GHL's POST body is intentionally small — the workflow
        itself carries the logic (triggers, steps, delays); we're
        just kicking it off for this contact. ``event_start_time``
        is optional and only matters for workflows whose first
        step is a time-based delay anchored on that event.
        """
        payload: dict[str, Any] = {}
        if event_start_time_iso:
            payload["eventStartTime"] = event_start_time_iso
        return await self._post(
            f"/contacts/{contact_id}/workflow/{workflow_id}",
            json=payload,
        )

    # ── tags ──────────────────────────────────────────────────

    async def tags_list(
        self, *, location_id: str,
    ) -> dict[str, Any]:
        return await self._get(
            f"/locations/{location_id}/tags",
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
            "ghl_api_key is not set — paste a GHL Private Integration "
            "Token in Settings → API Keys. Either an agency PIT or a "
            "sub-account custom integration key works; the sub-account "
            "key is the recommended default."
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
