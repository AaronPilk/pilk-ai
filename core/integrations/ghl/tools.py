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


__all__ = ["make_ghl_pipeline_tools"]
