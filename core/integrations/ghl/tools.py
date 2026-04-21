"""Tool surface over :mod:`core.integrations.ghl.client`.

PR #75b — pipelines + opportunities (7 tools). Every tool follows
the same shape:

1. Validate arg types + presence.
2. :func:`resolve_location_id` picks the location scope.
3. :func:`client_from_settings` builds a fresh :class:`GHLClient`
   (resolves the PIT via ``resolve_secret`` on every call so a
   runtime rotation lands without a daemon restart).
4. Call the client method inside ``try/except GHLError`` and wrap
   the response in a :class:`ToolOutcome`.

The heavy lifting lives in the client; these are thin validators
+ formatters. Keeping that split makes adding tools cheap (~40 LoC
each) and tests for the HTTP surface separable from tests for the
tool surface.

### Risk classes

- ``ghl_pipeline_list`` — NET_READ
- ``ghl_opportunity_get`` / ``_search`` — NET_READ
- ``ghl_opportunity_create`` / ``_update`` / ``_move_stage`` /
  ``_delete`` — NET_WRITE (mutates CRM; approval queue by default)

``_delete`` is intentionally NET_WRITE not IRREVERSIBLE: GHL keeps
deleted opportunities in a recoverable trash state for a short
window, so an accidental delete can be undone from the GHL UI.
Escalate to IRREVERSIBLE if ever the API signature changes to
hard-delete.
"""

from __future__ import annotations

from typing import Any

from core.config import get_settings
from core.integrations.ghl.client import (
    GHLError,
    GHLNotConfiguredError,
    client_from_settings,
    resolve_location_id,
)
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.ghl.tools")


def _default_location() -> str | None:
    return get_settings().ghl_default_location_id


def _unwrap_location(
    args: dict[str, Any], tool_name: str,
) -> tuple[str | None, ToolOutcome | None]:
    """Resolve location_id for a tool call. Returns ``(loc, None)``
    on success or ``(None, error_outcome)`` when neither an arg nor
    the settings default is available — so the caller can early-
    return the user-facing error without raising.
    """
    try:
        loc = resolve_location_id(
            arg=args.get("location_id"),
            default=_default_location(),
        )
    except GHLError as e:
        return None, _surface(e, tool_name)
    return loc, None


def _surface(e: GHLError, tool_name: str) -> ToolOutcome:
    """Rewrite common failures into actionable copy. The raw body
    lives in ``data.raw`` so debugging keeps context."""
    hint = e.message
    if e.status == 401:
        hint = (
            f"{e.message} — ghl_api_key is invalid or missing. "
            "Reissue an agency PIT at Settings → Company → Private "
            "Integrations in GHL and paste into pilkd's Settings → "
            "API Keys."
        )
    elif e.status == 403:
        hint = (
            f"{e.message} — PIT is missing a scope. Reissue the "
            "token in GHL with every scope box checked."
        )
    elif e.status == 404:
        hint = (
            f"{e.message} — double-check the id; the location may "
            "also be wrong (pass location_id explicitly if the "
            "default doesn't match)."
        )
    elif e.status == 422:
        hint = f"Validation: {e.message}"
    elif e.status == 429:
        hint = (
            f"{e.message} — GHL rate limit; back off + retry."
        )
    return ToolOutcome(
        content=f"{tool_name} failed: GHL {e.status}: {hint}",
        is_error=True,
        data={"status": e.status, "raw": e.raw},
    )


def _not_configured(tool_name: str) -> ToolOutcome:
    return ToolOutcome(
        content=(
            f"{tool_name} failed: Go High Level not configured. "
            "Add ghl_api_key (agency PIT) in Settings → API Keys."
        ),
        is_error=True,
    )


# ── pipelines (read) ─────────────────────────────────────────────


def _pipeline_list_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        loc, err = _unwrap_location(args, "ghl_pipeline_list")
        if err:
            return err
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_pipeline_list")
        try:
            result = await client.pipelines_list(location_id=loc)
        except GHLError as e:
            return _surface(e, "ghl_pipeline_list")
        pipelines = result.get("pipelines") or []
        # Render a compact human summary the planner can reason
        # over without parsing JSON — pipeline name + stage names
        # in order — while keeping the raw payload in data.
        lines: list[str] = []
        for p in pipelines:
            name = p.get("name") or "(unnamed pipeline)"
            pid = p.get("id") or ""
            stages = p.get("stages") or []
            stage_labels = [
                f"{s.get('name', '?')} ({s.get('id', '')[:8]}…)"
                for s in stages
            ]
            lines.append(
                f"- {name} [{pid}]\n    stages: "
                + " → ".join(stage_labels)
            )
        body = (
            "\n".join(lines)
            if lines else "No pipelines configured for this location."
        )
        return ToolOutcome(
            content=f"{len(pipelines)} pipeline(s):\n{body}",
            data={"pipelines": pipelines, "location_id": loc},
        )

    return Tool(
        name="ghl_pipeline_list",
        description=(
            "List every sales pipeline configured in the Go High "
            "Level location, with each pipeline's ordered stages "
            "and ids. Use before creating an opportunity (you need "
            "the pipeline + starting stage id) or before calling "
            "ghl_opportunity_move_stage. Pass `location_id` to "
            "target a specific sub-account; omit to use the "
            "default."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "location_id": {
                    "type": "string",
                    "description": (
                        "GHL sub-account id. Omit to use "
                        "ghl_default_location_id from settings."
                    ),
                },
            },
        },
        risk=RiskClass.NET_READ,
        handler=handler,
    )


# ── opportunities (create / get / search / update / move / delete) ────


def _opportunity_create_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        pipeline_id = str(args.get("pipeline_id") or "").strip()
        pipeline_stage_id = str(args.get("pipeline_stage_id") or "").strip()
        contact_id = str(args.get("contact_id") or "").strip()
        name = str(args.get("name") or "").strip()
        if not pipeline_id:
            return ToolOutcome(
                content=(
                    "ghl_opportunity_create requires 'pipeline_id'. "
                    "Call ghl_pipeline_list first to find it."
                ),
                is_error=True,
            )
        if not pipeline_stage_id:
            return ToolOutcome(
                content=(
                    "ghl_opportunity_create requires "
                    "'pipeline_stage_id'. Pick one from the pipeline "
                    "returned by ghl_pipeline_list."
                ),
                is_error=True,
            )
        if not contact_id:
            return ToolOutcome(
                content=(
                    "ghl_opportunity_create requires 'contact_id'. "
                    "Every opportunity in GHL is tied to a contact."
                ),
                is_error=True,
            )
        if not name:
            return ToolOutcome(
                content=(
                    "ghl_opportunity_create requires a 'name' — "
                    "the deal title shown in the pipeline view."
                ),
                is_error=True,
            )
        loc, err = _unwrap_location(args, "ghl_opportunity_create")
        if err:
            return err
        payload: dict[str, Any] = {
            "pipelineId": pipeline_id,
            "pipelineStageId": pipeline_stage_id,
            "contactId": contact_id,
            "name": name,
        }
        status = args.get("status")
        if isinstance(status, str) and status.strip():
            payload["status"] = status.strip()
        monetary_value = args.get("monetary_value")
        if isinstance(monetary_value, int | float):
            payload["monetaryValue"] = monetary_value
        assigned_to = args.get("assigned_to")
        if isinstance(assigned_to, str) and assigned_to.strip():
            payload["assignedTo"] = assigned_to.strip()
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_opportunity_create")
        try:
            result = await client.opportunities_create(
                location_id=loc, payload=payload,
            )
        except GHLError as e:
            return _surface(e, "ghl_opportunity_create")
        opp = result.get("opportunity") or result
        opp_id = opp.get("id") or ""
        return ToolOutcome(
            content=(
                f"Created opportunity '{name}' "
                f"({opp_id[:12]}…) in pipeline stage "
                f"{pipeline_stage_id[:8]}…"
            ),
            data={
                "opportunity_id": opp_id,
                "pipeline_id": pipeline_id,
                "pipeline_stage_id": pipeline_stage_id,
                "contact_id": contact_id,
                "name": name,
                "raw": result,
            },
        )

    return Tool(
        name="ghl_opportunity_create",
        description=(
            "Create a new opportunity (deal) in a GHL pipeline. "
            "Opportunities track a contact moving through a sales "
            "pipeline's stages. Requires pipeline_id, "
            "pipeline_stage_id (both from ghl_pipeline_list), "
            "contact_id (from a prior search / create), and name. "
            "Optional: status ('open' | 'won' | 'lost' | "
            "'abandoned'), monetary_value, assigned_to (user id). "
            "NET_WRITE — queues for approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pipeline_id": {"type": "string"},
                "pipeline_stage_id": {"type": "string"},
                "contact_id": {"type": "string"},
                "name": {"type": "string"},
                "status": {"type": "string"},
                "monetary_value": {"type": "number"},
                "assigned_to": {"type": "string"},
                "location_id": {"type": "string"},
            },
            "required": [
                "pipeline_id",
                "pipeline_stage_id",
                "contact_id",
                "name",
            ],
        },
        risk=RiskClass.NET_WRITE,
        handler=handler,
    )


def _opportunity_get_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        opp_id = str(args.get("opportunity_id") or "").strip()
        if not opp_id:
            return ToolOutcome(
                content="ghl_opportunity_get requires 'opportunity_id'.",
                is_error=True,
            )
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_opportunity_get")
        try:
            result = await client.opportunities_get(opp_id)
        except GHLError as e:
            return _surface(e, "ghl_opportunity_get")
        opp = result.get("opportunity") or result
        return ToolOutcome(
            content=(
                f"{opp.get('name', '(unnamed)')} "
                f"[{opp.get('status', 'open')}] "
                f"pipeline_stage={opp.get('pipelineStageId', '')[:8]}… "
                f"value={opp.get('monetaryValue', 0)}"
            ),
            data={"opportunity": opp},
        )

    return Tool(
        name="ghl_opportunity_get",
        description=(
            "Fetch a single opportunity by id. Returns the deal's "
            "name, status, pipeline stage, monetary value, "
            "assigned user, and contact id. Use when you have the "
            "id from search / create and need current state."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "opportunity_id": {"type": "string"},
            },
            "required": ["opportunity_id"],
        },
        risk=RiskClass.NET_READ,
        handler=handler,
    )


def _opportunity_search_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        loc, err = _unwrap_location(args, "ghl_opportunity_search")
        if err:
            return err
        try:
            limit = int(args.get("limit") or 25)
        except (TypeError, ValueError):
            limit = 25
        limit = max(1, min(limit, 100))
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_opportunity_search")
        try:
            result = await client.opportunities_search(
                location_id=loc,
                query=args.get("query"),
                pipeline_id=args.get("pipeline_id"),
                pipeline_stage_id=args.get("pipeline_stage_id"),
                status=args.get("status"),
                assigned_to=args.get("assigned_to"),
                limit=limit,
            )
        except GHLError as e:
            return _surface(e, "ghl_opportunity_search")
        opps = result.get("opportunities") or []
        lines = [
            (
                f"- {o.get('name', '(unnamed)')} "
                f"[{o.get('status', 'open')}] "
                f"${o.get('monetaryValue', 0)} "
                f"(id={o.get('id', '')[:12]}…)"
            )
            for o in opps
        ]
        body = "\n".join(lines) if lines else "No opportunities match."
        return ToolOutcome(
            content=f"{len(opps)} opportunity/ies:\n{body}",
            data={
                "opportunities": opps,
                "location_id": loc,
                "total": result.get("total"),
            },
        )

    return Tool(
        name="ghl_opportunity_search",
        description=(
            "Search opportunities in a GHL location. Every filter is "
            "optional: query (substring match on name / notes), "
            "pipeline_id, pipeline_stage_id, status ('open' | 'won' "
            "| 'lost' | 'abandoned'), assigned_to (user id), limit "
            "(1-100, default 25). Returns opportunities with id, "
            "name, status, value, stage. NET_READ — first call may "
            "queue for approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "pipeline_stage_id": {"type": "string"},
                "status": {"type": "string"},
                "assigned_to": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                },
                "location_id": {"type": "string"},
            },
        },
        risk=RiskClass.NET_READ,
        handler=handler,
    )


def _opportunity_update_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        opp_id = str(args.get("opportunity_id") or "").strip()
        if not opp_id:
            return ToolOutcome(
                content=(
                    "ghl_opportunity_update requires 'opportunity_id'."
                ),
                is_error=True,
            )
        payload: dict[str, Any] = {}
        for arg_key, body_key in (
            ("name", "name"),
            ("status", "status"),
            ("pipeline_id", "pipelineId"),
            ("pipeline_stage_id", "pipelineStageId"),
            ("assigned_to", "assignedTo"),
        ):
            raw = args.get(arg_key)
            if isinstance(raw, str) and raw.strip():
                payload[body_key] = raw.strip()
        monetary_value = args.get("monetary_value")
        if isinstance(monetary_value, int | float):
            payload["monetaryValue"] = monetary_value
        if not payload:
            return ToolOutcome(
                content=(
                    "ghl_opportunity_update needs at least one "
                    "field to change (name / status / pipeline_id / "
                    "pipeline_stage_id / monetary_value / assigned_to)."
                ),
                is_error=True,
            )
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_opportunity_update")
        try:
            result = await client.opportunities_update(
                opp_id, payload=payload,
            )
        except GHLError as e:
            return _surface(e, "ghl_opportunity_update")
        changed = ", ".join(sorted(payload.keys()))
        return ToolOutcome(
            content=f"Updated opportunity {opp_id[:12]}… ({changed}).",
            data={
                "opportunity_id": opp_id,
                "changed_fields": sorted(payload.keys()),
                "raw": result,
            },
        )

    return Tool(
        name="ghl_opportunity_update",
        description=(
            "Update fields on an existing opportunity. Any field "
            "can be changed: name, status, pipeline_id, "
            "pipeline_stage_id (to advance / regress), "
            "monetary_value, assigned_to. Only pass the ones you're "
            "changing. For the common 'move the deal to the next "
            "stage' call, use ghl_opportunity_move_stage instead — "
            "it's a tighter API for the planner to reason over. "
            "NET_WRITE — queues for approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "opportunity_id": {"type": "string"},
                "name": {"type": "string"},
                "status": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "pipeline_stage_id": {"type": "string"},
                "monetary_value": {"type": "number"},
                "assigned_to": {"type": "string"},
            },
            "required": ["opportunity_id"],
        },
        risk=RiskClass.NET_WRITE,
        handler=handler,
    )


def _opportunity_move_stage_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        opp_id = str(args.get("opportunity_id") or "").strip()
        stage_id = str(args.get("pipeline_stage_id") or "").strip()
        if not opp_id:
            return ToolOutcome(
                content=(
                    "ghl_opportunity_move_stage requires "
                    "'opportunity_id'."
                ),
                is_error=True,
            )
        if not stage_id:
            return ToolOutcome(
                content=(
                    "ghl_opportunity_move_stage requires "
                    "'pipeline_stage_id' (the stage to move to). "
                    "Call ghl_pipeline_list to see available stages."
                ),
                is_error=True,
            )
        pipeline_id_raw = args.get("pipeline_id")
        pipeline_id = (
            str(pipeline_id_raw).strip()
            if isinstance(pipeline_id_raw, str) and pipeline_id_raw.strip()
            else None
        )
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_opportunity_move_stage")
        try:
            result = await client.opportunities_move_stage(
                opp_id,
                pipeline_stage_id=stage_id,
                pipeline_id=pipeline_id,
            )
        except GHLError as e:
            return _surface(e, "ghl_opportunity_move_stage")
        return ToolOutcome(
            content=(
                f"Moved opportunity {opp_id[:12]}… to stage "
                f"{stage_id[:8]}…"
            ),
            data={
                "opportunity_id": opp_id,
                "pipeline_stage_id": stage_id,
                "pipeline_id": pipeline_id,
                "raw": result,
            },
        )

    return Tool(
        name="ghl_opportunity_move_stage",
        description=(
            "Advance (or regress) an opportunity to a different "
            "pipeline stage — the most common 'move this deal "
            "forward' operation. Pass opportunity_id + target "
            "pipeline_stage_id. Include pipeline_id when the target "
            "stage belongs to a different pipeline than the deal "
            "currently sits in (GHL sometimes 422s without it). "
            "NET_WRITE — queues for approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "opportunity_id": {"type": "string"},
                "pipeline_stage_id": {"type": "string"},
                "pipeline_id": {"type": "string"},
            },
            "required": ["opportunity_id", "pipeline_stage_id"],
        },
        risk=RiskClass.NET_WRITE,
        handler=handler,
    )


def _opportunity_delete_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        opp_id = str(args.get("opportunity_id") or "").strip()
        if not opp_id:
            return ToolOutcome(
                content=(
                    "ghl_opportunity_delete requires 'opportunity_id'."
                ),
                is_error=True,
            )
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_opportunity_delete")
        try:
            await client.opportunities_delete(opp_id)
        except GHLError as e:
            return _surface(e, "ghl_opportunity_delete")
        return ToolOutcome(
            content=f"Deleted opportunity {opp_id[:12]}… (in GHL trash).",
            data={"opportunity_id": opp_id, "deleted": True},
        )

    return Tool(
        name="ghl_opportunity_delete",
        description=(
            "Delete an opportunity by id. GHL keeps deleted "
            "opportunities in a recoverable trash state for a short "
            "window — recoverable from the GHL UI if needed. "
            "NET_WRITE — queues for approval. Don't use for "
            "'this deal is lost': update status to 'lost' instead "
            "via ghl_opportunity_update so the history stays in "
            "the pipeline."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "opportunity_id": {"type": "string"},
            },
            "required": ["opportunity_id"],
        },
        risk=RiskClass.NET_WRITE,
        handler=handler,
    )


# ── factory ──────────────────────────────────────────────────────


def make_ghl_pipeline_tools() -> list[Tool]:
    """Every pipeline / opportunity tool in the current rollout.

    PR #75b ships these 7; later PRs add contacts, conversations,
    calendars, and workflows via sibling factories.
    """
    return [
        _pipeline_list_tool(),
        _opportunity_create_tool(),
        _opportunity_get_tool(),
        _opportunity_search_tool(),
        _opportunity_update_tool(),
        _opportunity_move_stage_tool(),
        _opportunity_delete_tool(),
    ]


# ── contacts (PR #75c) ───────────────────────────────────────────
#
# Eight contact tools cover the full CRUD + the two relationship
# surfaces that matter at a CRM level (tags + notes). Naming
# convention: ``ghl_contact_<verb>`` so the planner picks them by
# intent the same way it picks the pipeline tools.
#
# Risk classes:
#   READ on get / search
#   WRITE on create / update / delete / add_tag / remove_tag /
#         add_note (every mutation queues for approval by default)
#
# Validation pattern: every required field checked up-front before
# we even resolve the API key, so a malformed call surfaces the
# real error instead of "not configured" or a 422 round-trip.


def _contact_create_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        # GHL accepts contacts with no required field, but at least
        # one identifier (email or phone) is needed for the contact
        # to be useful. Refusing the no-identifier case here saves
        # a wasted CRM row.
        email = str(args.get("email") or "").strip()
        phone = str(args.get("phone") or "").strip()
        first_name = str(args.get("first_name") or "").strip()
        last_name = str(args.get("last_name") or "").strip()
        name = str(args.get("name") or "").strip()
        company_name = str(args.get("company_name") or "").strip()
        if not (email or phone):
            return ToolOutcome(
                content=(
                    "ghl_contact_create requires at least one of "
                    "'email' or 'phone' so the contact is reachable."
                ),
                is_error=True,
            )
        loc, err = _unwrap_location(args, "ghl_contact_create")
        if err:
            return err
        payload: dict[str, Any] = {}
        if email:
            payload["email"] = email
        if phone:
            payload["phone"] = phone
        if first_name:
            payload["firstName"] = first_name
        if last_name:
            payload["lastName"] = last_name
        if name:
            payload["name"] = name
        if company_name:
            payload["companyName"] = company_name
        tags = args.get("tags")
        if isinstance(tags, list) and tags:
            payload["tags"] = [str(t).strip() for t in tags if str(t).strip()]
        custom = args.get("custom_fields")
        if isinstance(custom, list) and custom:
            payload["customFields"] = custom
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_contact_create")
        try:
            result = await client.contacts_create(
                location_id=loc, payload=payload,
            )
        except GHLError as e:
            return _surface(e, "ghl_contact_create")
        contact = result.get("contact") or result
        cid = contact.get("id") or ""
        label = (
            f"{first_name} {last_name}".strip()
            or name
            or email
            or phone
        )
        return ToolOutcome(
            content=(
                f"Created contact '{label}' "
                f"({cid[:12]}…) in location {loc[:8]}…"
            ),
            data={
                "contact_id": cid,
                "email": email,
                "phone": phone,
                "raw": result,
            },
        )

    return Tool(
        name="ghl_contact_create",
        description=(
            "Create a new contact in a GHL location. At least one of "
            "'email' or 'phone' is required (otherwise the contact "
            "has no way to be reached). Optional: first_name, "
            "last_name, name, company_name, tags (list of strings), "
            "custom_fields (list of {id, field_value} per GHL's "
            "custom-field schema). NET_WRITE — queues for approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "name": {"type": "string"},
                "company_name": {"type": "string"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "custom_fields": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "location_id": {"type": "string"},
            },
        },
        risk=RiskClass.NET_WRITE,
        handler=handler,
    )


def _contact_get_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        contact_id = str(args.get("contact_id") or "").strip()
        if not contact_id:
            return ToolOutcome(
                content="ghl_contact_get requires 'contact_id'.",
                is_error=True,
            )
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_contact_get")
        try:
            result = await client.contacts_get(contact_id)
        except GHLError as e:
            return _surface(e, "ghl_contact_get")
        contact = result.get("contact") or result
        return ToolOutcome(
            content=(
                f"{contact.get('firstName', '') or ''} "
                f"{contact.get('lastName', '') or ''}".strip()
                + f" · {contact.get('email', '') or ''}"
                + f" · {contact.get('phone', '') or ''}"
                + f" (tags: {', '.join(contact.get('tags', []) or []) or 'none'})"
            ),
            data={"contact": contact},
        )

    return Tool(
        name="ghl_contact_get",
        description=(
            "Fetch one contact by id. Returns name, email, phone, "
            "company, tags, and custom fields. Use when you have an "
            "id from search / create and need the full record."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
            },
            "required": ["contact_id"],
        },
        risk=RiskClass.NET_READ,
        handler=handler,
    )


def _contact_search_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        loc, err = _unwrap_location(args, "ghl_contact_search")
        if err:
            return err
        try:
            limit = int(args.get("limit") or 25)
        except (TypeError, ValueError):
            limit = 25
        limit = max(1, min(limit, 100))
        # email > phone > query precedence (matches client-side order
        # — exact match wins over fuzzy).
        email = str(args.get("email") or "").strip() or None
        phone = str(args.get("phone") or "").strip() or None
        query = str(args.get("query") or "").strip() or None
        if not (email or phone or query):
            return ToolOutcome(
                content=(
                    "ghl_contact_search requires at least one of "
                    "'email', 'phone', or 'query'."
                ),
                is_error=True,
            )
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_contact_search")
        try:
            result = await client.contacts_search(
                location_id=loc,
                email=email,
                phone=phone,
                query=query,
                limit=limit,
            )
        except GHLError as e:
            return _surface(e, "ghl_contact_search")
        contacts = result.get("contacts") or []
        lines = [
            (
                f"- {c.get('firstName', '') or ''} "
                f"{c.get('lastName', '') or ''}".strip()
                + f" · {c.get('email', '') or '(no email)'}"
                + f" · id={c.get('id', '')[:12]}…"
            )
            for c in contacts
        ]
        body = "\n".join(lines) if lines else "No contacts match."
        return ToolOutcome(
            content=f"{len(contacts)} contact(s):\n{body}",
            data={
                "contacts": contacts,
                "location_id": loc,
                "total": result.get("total"),
            },
        )

    return Tool(
        name="ghl_contact_search",
        description=(
            "Search contacts in a GHL location. At least one of: "
            "email (exact match, fastest), phone (exact match), or "
            "query (substring across name + email). limit 1-100, "
            "default 25. NET_READ — first call may queue for approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "query": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                },
                "location_id": {"type": "string"},
            },
        },
        risk=RiskClass.NET_READ,
        handler=handler,
    )


def _contact_update_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        contact_id = str(args.get("contact_id") or "").strip()
        if not contact_id:
            return ToolOutcome(
                content="ghl_contact_update requires 'contact_id'.",
                is_error=True,
            )
        payload: dict[str, Any] = {}
        # Snake-case → GHL camelCase.
        for arg_key, body_key in (
            ("email", "email"),
            ("phone", "phone"),
            ("first_name", "firstName"),
            ("last_name", "lastName"),
            ("name", "name"),
            ("company_name", "companyName"),
        ):
            raw = args.get(arg_key)
            if isinstance(raw, str) and raw.strip():
                payload[body_key] = raw.strip()
        tags = args.get("tags")
        if isinstance(tags, list):
            payload["tags"] = [str(t).strip() for t in tags if str(t).strip()]
        custom = args.get("custom_fields")
        if isinstance(custom, list):
            payload["customFields"] = custom
        if not payload:
            return ToolOutcome(
                content=(
                    "ghl_contact_update needs at least one field to "
                    "change (email / phone / first_name / last_name / "
                    "name / company_name / tags / custom_fields). "
                    "For tag-only changes prefer ghl_contact_add_tag / "
                    "remove_tag — they're additive instead of "
                    "destructive."
                ),
                is_error=True,
            )
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_contact_update")
        try:
            result = await client.contacts_update(
                contact_id, payload=payload,
            )
        except GHLError as e:
            return _surface(e, "ghl_contact_update")
        return ToolOutcome(
            content=(
                f"Updated contact {contact_id[:12]}… "
                f"({', '.join(sorted(payload.keys()))})."
            ),
            data={
                "contact_id": contact_id,
                "changed_fields": sorted(payload.keys()),
                "raw": result,
            },
        )

    return Tool(
        name="ghl_contact_update",
        description=(
            "Update fields on an existing contact. Pass only the "
            "ones you're changing. NOTE: passing 'tags' REPLACES "
            "the entire tag list — for additive changes use "
            "ghl_contact_add_tag / ghl_contact_remove_tag instead. "
            "NET_WRITE — queues for approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "name": {"type": "string"},
                "company_name": {"type": "string"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "REPLACES existing tags. Use add_tag / "
                        "remove_tag for additive changes."
                    ),
                },
                "custom_fields": {
                    "type": "array",
                    "items": {"type": "object"},
                },
            },
            "required": ["contact_id"],
        },
        risk=RiskClass.NET_WRITE,
        handler=handler,
    )


def _contact_delete_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        contact_id = str(args.get("contact_id") or "").strip()
        if not contact_id:
            return ToolOutcome(
                content="ghl_contact_delete requires 'contact_id'.",
                is_error=True,
            )
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_contact_delete")
        try:
            await client.contacts_delete(contact_id)
        except GHLError as e:
            return _surface(e, "ghl_contact_delete")
        return ToolOutcome(
            content=f"Deleted contact {contact_id[:12]}….",
            data={"contact_id": contact_id, "deleted": True},
        )

    return Tool(
        name="ghl_contact_delete",
        description=(
            "Delete a contact by id. GHL keeps deleted contacts in "
            "trash for a short window (recoverable from the GHL UI). "
            "NET_WRITE — queues for approval. For 'this lead went "
            "cold' situations, prefer ghl_contact_remove_tag to drop "
            "their hot/active tags so the contact stays in the "
            "history."
        ),
        input_schema={
            "type": "object",
            "properties": {"contact_id": {"type": "string"}},
            "required": ["contact_id"],
        },
        risk=RiskClass.NET_WRITE,
        handler=handler,
    )


def _contact_add_tag_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        contact_id = str(args.get("contact_id") or "").strip()
        if not contact_id:
            return ToolOutcome(
                content="ghl_contact_add_tag requires 'contact_id'.",
                is_error=True,
            )
        tags_raw = args.get("tags")
        if not isinstance(tags_raw, list) or not tags_raw:
            return ToolOutcome(
                content=(
                    "ghl_contact_add_tag requires a non-empty 'tags' "
                    "list of strings."
                ),
                is_error=True,
            )
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
        if not tags:
            return ToolOutcome(
                content=(
                    "ghl_contact_add_tag 'tags' had no non-empty "
                    "values."
                ),
                is_error=True,
            )
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_contact_add_tag")
        try:
            result = await client.contacts_add_tags(
                contact_id, tags=tags,
            )
        except GHLError as e:
            return _surface(e, "ghl_contact_add_tag")
        return ToolOutcome(
            content=(
                f"Added {len(tags)} tag(s) to contact "
                f"{contact_id[:12]}…: {', '.join(tags)}"
            ),
            data={
                "contact_id": contact_id,
                "tags_added": tags,
                "raw": result,
            },
        )

    return Tool(
        name="ghl_contact_add_tag",
        description=(
            "Add one or more tags to a contact. Additive — existing "
            "tags are preserved. Tag names are case-sensitive in GHL "
            "and create-if-missing (no separate 'create tag' call). "
            "NET_WRITE — queues for approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
            },
            "required": ["contact_id", "tags"],
        },
        risk=RiskClass.NET_WRITE,
        handler=handler,
    )


def _contact_remove_tag_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        contact_id = str(args.get("contact_id") or "").strip()
        if not contact_id:
            return ToolOutcome(
                content=(
                    "ghl_contact_remove_tag requires 'contact_id'."
                ),
                is_error=True,
            )
        tags_raw = args.get("tags")
        if not isinstance(tags_raw, list) or not tags_raw:
            return ToolOutcome(
                content=(
                    "ghl_contact_remove_tag requires a non-empty "
                    "'tags' list of strings."
                ),
                is_error=True,
            )
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
        if not tags:
            return ToolOutcome(
                content=(
                    "ghl_contact_remove_tag 'tags' had no non-empty "
                    "values."
                ),
                is_error=True,
            )
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_contact_remove_tag")
        try:
            result = await client.contacts_remove_tags(
                contact_id, tags=tags,
            )
        except GHLError as e:
            return _surface(e, "ghl_contact_remove_tag")
        return ToolOutcome(
            content=(
                f"Removed {len(tags)} tag(s) from contact "
                f"{contact_id[:12]}…: {', '.join(tags)}"
            ),
            data={
                "contact_id": contact_id,
                "tags_removed": tags,
                "raw": result,
            },
        )

    return Tool(
        name="ghl_contact_remove_tag",
        description=(
            "Remove one or more tags from a contact. Other tags are "
            "preserved. Tag names not currently on the contact are "
            "silently ignored (GHL doesn't error). NET_WRITE — "
            "queues for approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
            },
            "required": ["contact_id", "tags"],
        },
        risk=RiskClass.NET_WRITE,
        handler=handler,
    )


def _contact_add_note_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        contact_id = str(args.get("contact_id") or "").strip()
        body = str(args.get("body") or "").strip()
        if not contact_id:
            return ToolOutcome(
                content="ghl_contact_add_note requires 'contact_id'.",
                is_error=True,
            )
        if not body:
            return ToolOutcome(
                content=(
                    "ghl_contact_add_note requires a non-empty "
                    "'body' — the note text shown on the contact's "
                    "timeline in GHL."
                ),
                is_error=True,
            )
        user_id_raw = args.get("user_id")
        user_id = (
            str(user_id_raw).strip()
            if isinstance(user_id_raw, str) and user_id_raw.strip()
            else None
        )
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_contact_add_note")
        try:
            result = await client.contacts_add_note(
                contact_id, body=body, user_id=user_id,
            )
        except GHLError as e:
            return _surface(e, "ghl_contact_add_note")
        note = result.get("note") or result
        nid = note.get("id") or ""
        return ToolOutcome(
            content=(
                f"Added note ({nid[:12]}…) to contact "
                f"{contact_id[:12]}…: {body[:80]}"
            ),
            data={
                "contact_id": contact_id,
                "note_id": nid,
                "raw": result,
            },
        )

    return Tool(
        name="ghl_contact_add_note",
        description=(
            "Add a free-text note to a contact's timeline in GHL. "
            "Use for 'spoke with them at 3pm', 'left voicemail', "
            "'wants pricing breakdown' — anything that should appear "
            "in the contact's history alongside other CRM activity. "
            "Optional 'user_id' attributes the note to a specific GHL "
            "user (call ghl_user_list to find one); omit and the note "
            "is attributed to the integration. NET_WRITE — queues for "
            "approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "body": {"type": "string"},
                "user_id": {"type": "string"},
            },
            "required": ["contact_id", "body"],
        },
        risk=RiskClass.NET_WRITE,
        handler=handler,
    )


# ── meta (PR #75c) ───────────────────────────────────────────────
#
# Three small reads that let the planner discover the IDs every
# other tool needs. ``ghl_location_list`` is essential for agency
# accounts; the other two are convenience reads that save a planner
# turn looking IDs up from the GHL UI.


def _location_list_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        try:
            limit = int(args.get("limit") or 100)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        company_raw = args.get("company_id")
        company_id = (
            str(company_raw).strip()
            if isinstance(company_raw, str) and company_raw.strip()
            else None
        )
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_location_list")
        try:
            result = await client.locations_list(
                company_id=company_id, limit=limit,
            )
        except GHLError as e:
            return _surface(e, "ghl_location_list")
        locations = result.get("locations") or []
        lines = [
            f"- {loc.get('name', '(unnamed)')} ({loc.get('id', '')})"
            for loc in locations
        ]
        body = (
            "\n".join(lines)
            if lines
            else "No locations visible to this PIT (agency-level "
                 "tokens see all sub-accounts; location PITs see "
                 "only their own)."
        )
        return ToolOutcome(
            content=f"{len(locations)} location(s):\n{body}",
            data={"locations": locations},
        )

    return Tool(
        name="ghl_location_list",
        description=(
            "List GHL sub-accounts (locations) the agency PIT can "
            "see. Agency-level tokens return every location under "
            "the agency; location-scoped tokens return only their "
            "own. Use to discover location IDs for the optional "
            "'location_id' arg on every other tool. Optional "
            "company_id narrows further when the PIT spans "
            "multiple companies (rare). NET_READ."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                },
            },
        },
        risk=RiskClass.NET_READ,
        handler=handler,
    )


def _user_list_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        loc, err = _unwrap_location(args, "ghl_user_list")
        if err:
            return err
        try:
            limit = int(args.get("limit") or 100)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_user_list")
        try:
            result = await client.users_list(
                location_id=loc, limit=limit,
            )
        except GHLError as e:
            return _surface(e, "ghl_user_list")
        users = result.get("users") or []
        lines = [
            (
                f"- {u.get('firstName', '') or ''} "
                f"{u.get('lastName', '') or ''}".strip()
                + f" · {u.get('email', '') or '(no email)'}"
                + f" · id={u.get('id', '')[:12]}…"
                + (
                    f" · roles={', '.join(u.get('roles', {}).get('role', []))}"
                    if u.get("roles") else ""
                )
            )
            for u in users
        ]
        body = "\n".join(lines) if lines else "No users."
        return ToolOutcome(
            content=f"{len(users)} user(s):\n{body}",
            data={"users": users, "location_id": loc},
        )

    return Tool(
        name="ghl_user_list",
        description=(
            "List GHL users in a location. Use to find a user_id "
            "for assigning opportunities, attributing notes, or "
            "routing conversations. Returns name, email, role(s), "
            "id. NET_READ."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "location_id": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                },
            },
        },
        risk=RiskClass.NET_READ,
        handler=handler,
    )


def _custom_field_list_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        loc, err = _unwrap_location(args, "ghl_custom_field_list")
        if err:
            return err
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_custom_field_list")
        try:
            result = await client.custom_fields_list(location_id=loc)
        except GHLError as e:
            return _surface(e, "ghl_custom_field_list")
        fields = result.get("customFields") or []
        lines = [
            (
                f"- {f.get('name', '(unnamed)')} "
                f"[{f.get('dataType', '?')}] id={f.get('id', '')[:12]}…"
            )
            for f in fields
        ]
        body = "\n".join(lines) if lines else "No custom fields."
        return ToolOutcome(
            content=f"{len(fields)} custom field(s):\n{body}",
            data={"custom_fields": fields, "location_id": loc},
        )

    return Tool(
        name="ghl_custom_field_list",
        description=(
            "List a location's custom fields with id, name, data "
            "type. Use to discover the field id + value shape "
            "before passing 'custom_fields' to ghl_contact_create / "
            "update. NET_READ."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "location_id": {"type": "string"},
            },
        },
        risk=RiskClass.NET_READ,
        handler=handler,
    )


def make_ghl_contact_tools() -> list[Tool]:
    """Contacts CRUD + meta tools.

    PR #75c ships these 11; sibling factories (pipelines, future
    conversations / calendars / workflows) are kept separate so the
    lifespan can register subsets without one factory's failure
    breaking the others.
    """
    return [
        _contact_create_tool(),
        _contact_get_tool(),
        _contact_search_tool(),
        _contact_update_tool(),
        _contact_delete_tool(),
        _contact_add_tag_tool(),
        _contact_remove_tag_tool(),
        _contact_add_note_tool(),
        _location_list_tool(),
        _user_list_tool(),
        _custom_field_list_tool(),
    ]


# ── conversations (PR #75d) ──────────────────────────────────────
#
# Four tools covering the operator's day-to-day outbound +
# inbound conversation surface: send SMS, send email, search
# conversations, read a thread.
#
# Risk classes: sends are COMMS (every outbound message lands in
# the approval queue by default — same posture as gmail_send_as_me
# and telegram_notify). Reads are NET_READ.


def _send_sms_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        contact_id = str(args.get("contact_id") or "").strip()
        message = str(args.get("message") or "").strip()
        if not contact_id:
            return ToolOutcome(
                content="ghl_send_sms requires 'contact_id'.",
                is_error=True,
            )
        if not message:
            return ToolOutcome(
                content=(
                    "ghl_send_sms requires a non-empty 'message'."
                ),
                is_error=True,
            )
        if len(message) > 1600:
            # GHL segments past ~160 chars; past 1600 the operator is
            # almost certainly not composing an SMS. Cut early to
            # avoid an accidental 10-segment charge.
            return ToolOutcome(
                content=(
                    f"ghl_send_sms message too long ({len(message)} "
                    "chars). SMS splits at 160-char boundaries; past "
                    "1600 chars use email or a document link."
                ),
                is_error=True,
            )
        loc, err = _unwrap_location(args, "ghl_send_sms")
        if err:
            return err
        from_number_raw = args.get("from_number")
        from_number = (
            str(from_number_raw).strip()
            if isinstance(from_number_raw, str) and from_number_raw.strip()
            else None
        )
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_send_sms")
        try:
            result = await client.conversations_send_sms(
                contact_id=contact_id,
                location_id=loc,
                message=message,
                from_number=from_number,
            )
        except GHLError as e:
            return _surface(e, "ghl_send_sms")
        conv_id = result.get("conversationId") or ""
        msg_id = result.get("messageId") or ""
        return ToolOutcome(
            content=(
                f"Sent SMS to contact {contact_id[:12]}… "
                f"({len(message)} chars). "
                f"conv={conv_id[:12]}… msg={msg_id[:12]}…"
            ),
            data={
                "contact_id": contact_id,
                "conversation_id": conv_id,
                "message_id": msg_id,
                "chars": len(message),
            },
        )

    return Tool(
        name="ghl_send_sms",
        description=(
            "Send an SMS to a GHL contact through the location's "
            "configured phone number. Body goes out verbatim — GHL "
            "handles segmentation (160-char splits) but the operator "
            "is billed per segment. Optional 'from_number' overrides "
            "the location default when the sub-account has multiple "
            "numbers. COMMS risk — every send queues for approval "
            "by default."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "message": {
                    "type": "string",
                    "description": (
                        "SMS body. Hard cap 1600 chars (10 segments)."
                    ),
                },
                "from_number": {
                    "type": "string",
                    "description": (
                        "E.164 phone number owned by the location. "
                        "Omit to use the sub-account default."
                    ),
                },
                "location_id": {"type": "string"},
            },
            "required": ["contact_id", "message"],
        },
        risk=RiskClass.COMMS,
        handler=handler,
    )


def _send_email_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        contact_id = str(args.get("contact_id") or "").strip()
        subject = str(args.get("subject") or "").strip()
        html = args.get("html")
        text = args.get("text")
        html_str = html.strip() if isinstance(html, str) else ""
        text_str = text.strip() if isinstance(text, str) else ""
        if not contact_id:
            return ToolOutcome(
                content="ghl_send_email requires 'contact_id'.",
                is_error=True,
            )
        if not subject:
            return ToolOutcome(
                content="ghl_send_email requires a 'subject'.",
                is_error=True,
            )
        if not (html_str or text_str):
            return ToolOutcome(
                content=(
                    "ghl_send_email requires at least one of 'html' "
                    "or 'text' (both is fine — GHL picks per-recipient "
                    "preferences)."
                ),
                is_error=True,
            )
        loc, err = _unwrap_location(args, "ghl_send_email")
        if err:
            return err
        from_email_raw = args.get("from_email")
        from_email = (
            str(from_email_raw).strip()
            if isinstance(from_email_raw, str) and from_email_raw.strip()
            else None
        )
        reply_to_raw = args.get("reply_to")
        reply_to = (
            str(reply_to_raw).strip()
            if isinstance(reply_to_raw, str) and reply_to_raw.strip()
            else None
        )
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_send_email")
        try:
            result = await client.conversations_send_email(
                contact_id=contact_id,
                location_id=loc,
                subject=subject,
                html=html_str or None,
                text=text_str or None,
                from_email=from_email,
                reply_to=reply_to,
            )
        except GHLError as e:
            return _surface(e, "ghl_send_email")
        conv_id = result.get("conversationId") or ""
        msg_id = result.get("messageId") or ""
        return ToolOutcome(
            content=(
                f"Sent email to contact {contact_id[:12]}… "
                f"(subject: {subject}). "
                f"conv={conv_id[:12]}… msg={msg_id[:12]}…"
            ),
            data={
                "contact_id": contact_id,
                "conversation_id": conv_id,
                "message_id": msg_id,
                "subject": subject,
            },
        )

    return Tool(
        name="ghl_send_email",
        description=(
            "Send an email to a GHL contact through the location's "
            "email channel. Requires subject + at least one of html "
            "/ text (both is fine). Optional from_email + reply_to "
            "override the sub-account default sending identity. "
            "COMMS risk — every send queues for approval by default. "
            "Use this when the operator wants an email tracked in "
            "the GHL timeline; use gmail_send_as_me when the email "
            "should land from their personal Gmail instead."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "subject": {"type": "string"},
                "html": {
                    "type": "string",
                    "description": "HTML body. At least one of html/text required.",
                },
                "text": {
                    "type": "string",
                    "description": "Plain-text body. At least one of html/text required.",
                },
                "from_email": {"type": "string"},
                "reply_to": {"type": "string"},
                "location_id": {"type": "string"},
            },
            "required": ["contact_id", "subject"],
        },
        risk=RiskClass.COMMS,
        handler=handler,
    )


def _conversation_search_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        loc, err = _unwrap_location(args, "ghl_conversation_search")
        if err:
            return err
        try:
            limit = int(args.get("limit") or 25)
        except (TypeError, ValueError):
            limit = 25
        limit = max(1, min(limit, 100))
        contact_id_raw = args.get("contact_id")
        contact_id = (
            str(contact_id_raw).strip()
            if isinstance(contact_id_raw, str) and contact_id_raw.strip()
            else None
        )
        last_type_raw = args.get("last_message_type")
        last_type = (
            str(last_type_raw).strip()
            if isinstance(last_type_raw, str) and last_type_raw.strip()
            else None
        )
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_conversation_search")
        try:
            result = await client.conversations_search(
                location_id=loc,
                contact_id=contact_id,
                last_message_type=last_type,
                limit=limit,
            )
        except GHLError as e:
            return _surface(e, "ghl_conversation_search")
        conversations = result.get("conversations") or []
        lines = []
        for c in conversations:
            ts = c.get("lastMessageDate") or ""
            snippet = (c.get("lastMessageBody") or "")[:80]
            kind = c.get("lastMessageType") or "?"
            cid = c.get("id") or ""
            lines.append(
                f"- [{kind}] {ts[:16]} "
                f"({cid[:12]}…): {snippet}"
            )
        body = "\n".join(lines) if lines else "No conversations match."
        return ToolOutcome(
            content=f"{len(conversations)} conversation(s):\n{body}",
            data={
                "conversations": conversations,
                "location_id": loc,
                "total": result.get("total"),
            },
        )

    return Tool(
        name="ghl_conversation_search",
        description=(
            "Search conversation threads in a GHL location. Narrow "
            "by contact_id for 'history with this person', or by "
            "last_message_type ('SMS' | 'Email' | 'WhatsApp' | "
            "'FB' | 'IG' | 'GMB') for 'any unread <channel>?'. "
            "Returns threads sorted by last-message-date with a "
            "snippet of the most recent message. limit 1-100, "
            "default 25. NET_READ."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "last_message_type": {
                    "type": "string",
                    "description": (
                        "Channel filter: 'SMS' | 'Email' | "
                        "'WhatsApp' | 'FB' | 'IG' | 'GMB'."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                },
                "location_id": {"type": "string"},
            },
        },
        risk=RiskClass.NET_READ,
        handler=handler,
    )


def _conversation_get_messages_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext,
    ) -> ToolOutcome:
        conv_id = str(args.get("conversation_id") or "").strip()
        if not conv_id:
            return ToolOutcome(
                content=(
                    "ghl_conversation_get_messages requires "
                    "'conversation_id'."
                ),
                is_error=True,
            )
        try:
            limit = int(args.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 100))
        try:
            client = client_from_settings()
        except GHLNotConfiguredError:
            return _not_configured("ghl_conversation_get_messages")
        try:
            result = await client.conversations_get_messages(
                conv_id, limit=limit,
            )
        except GHLError as e:
            return _surface(e, "ghl_conversation_get_messages")
        raw_messages = result.get("messages")
        # GHL nests under {"messages": {"messages": [...], ...}} on
        # some endpoints; flatten defensively.
        if isinstance(raw_messages, dict):
            messages = raw_messages.get("messages") or []
        elif isinstance(raw_messages, list):
            messages = raw_messages
        else:
            messages = []
        lines = []
        for m in messages:
            ts = m.get("dateAdded") or ""
            kind = m.get("type") or "?"
            direction = (
                "→" if m.get("direction") == "outbound" else "←"
            )
            body_text = (m.get("body") or "").replace("\n", " ")[:120]
            lines.append(
                f"{ts[:16]} {direction} [{kind}] {body_text}"
            )
        rendered = "\n".join(lines) if lines else "(no messages)"
        return ToolOutcome(
            content=(
                f"{len(messages)} message(s) in conversation "
                f"{conv_id[:12]}…:\n\n{rendered}"
            ),
            data={
                "conversation_id": conv_id,
                "messages": messages,
            },
        )

    return Tool(
        name="ghl_conversation_get_messages",
        description=(
            "Fetch the last N messages in one conversation thread. "
            "Returns each message's timestamp, direction (inbound / "
            "outbound), channel, and body. Use after "
            "ghl_conversation_search to drill into a thread. "
            "limit 1-100, default 50. NET_READ."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": ["conversation_id"],
        },
        risk=RiskClass.NET_READ,
        handler=handler,
    )


def make_ghl_conversation_tools() -> list[Tool]:
    """Four conversation tools — SMS + email senders, search, thread read.

    PR #75d ships these alongside the existing pipeline + contact
    surfaces. Future channels (WhatsApp, Instagram DM, Messenger, GMB)
    can add sibling senders without restructuring the factory — each
    rides the same ``_send_conversation_message`` path on the client.
    """
    return [
        _send_sms_tool(),
        _send_email_tool(),
        _conversation_search_tool(),
        _conversation_get_messages_tool(),
    ]


__all__ = [
    "make_ghl_contact_tools",
    "make_ghl_conversation_tools",
    "make_ghl_pipeline_tools",
]
