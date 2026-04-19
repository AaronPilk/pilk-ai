"""PILK tool wrapper around :func:`ir_to_html`.

The tool reads a page dict (IR in JSON form), validates it via
Pydantic, runs the pure-function converter, and writes
``index.html`` + ``styles.css`` to the requested output directory.

Disk-only; no network. ``RiskClass.READ`` so the approval gate
doesn't pause the agent — the only side effect is two files. Callers
control the output directory, not the tool.

If the IR references a :class:`CanvaAssetSpec` (unresolved image),
the tool surfaces a clean ``is_error`` rather than writing a
half-rendered bundle. PR I adds the resolver; until then the agent
either supplies URL sources or defers to that PR.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.policy.risk import RiskClass
from core.tools.builtin.design.html import CanvaUnresolvedError, ir_to_html
from core.tools.builtin.design.ir import Page
from core.tools.registry import Tool, ToolContext, ToolOutcome


async def _handler(args: dict, ctx: ToolContext) -> ToolOutcome:
    raw = args.get("ir")
    if not isinstance(raw, dict):
        return ToolOutcome(
            content="html_export requires 'ir' as an object matching the Page schema.",
            is_error=True,
        )
    output_dir = str(args.get("output_dir") or "").strip()
    if not output_dir:
        return ToolOutcome(
            content="html_export requires 'output_dir' (absolute path).",
            is_error=True,
        )

    try:
        page = Page.model_validate(raw)
    except ValidationError as e:
        return ToolOutcome(
            content=f"invalid IR: {e.error_count()} validation error(s).",
            data={"errors": _short_errors(e)},
            is_error=True,
        )

    try:
        bundle = ir_to_html(page)
    except CanvaUnresolvedError as e:
        return ToolOutcome(content=f"refused: {e}", is_error=True)

    out = Path(output_dir).expanduser().resolve()
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text(bundle["index.html"], encoding="utf-8")
        (out / "styles.css").write_text(bundle["styles.css"], encoding="utf-8")
    except OSError as e:
        return ToolOutcome(
            content=f"could not write bundle to {out}: {e}", is_error=True
        )

    return ToolOutcome(
        content=(
            f"wrote {page.title} to {out} ({len(page.containers)} container(s))"
        ),
        data={
            "output_dir": str(out),
            "files": ["index.html", "styles.css"],
            "container_count": len(page.containers),
            "title": page.title,
            "slug": page.slug,
        },
    )


html_export_tool = Tool(
    name="html_export",
    description=(
        "Render a design-IR Page to a static HTML+Tailwind bundle. "
        "Writes index.html + styles.css into output_dir. Always use "
        "this to emit HTML — never generate markup by hand. "
        "Mobile-first by construction; responsive breakpoints at 768px "
        "(tablet) and 1024px (desktop) via Tailwind's md:/lg: prefixes."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "ir": {
                "type": "object",
                "description": (
                    "Page IR — see core/tools/builtin/design/README.md for "
                    "the schema. Must include title, slug, containers."
                ),
            },
            "output_dir": {
                "type": "string",
                "description": "Absolute filesystem path for the bundle.",
            },
        },
        "required": ["ir", "output_dir"],
    },
    risk=RiskClass.READ,
    handler=_handler,
)


def _short_errors(e: ValidationError) -> list[dict[str, Any]]:
    """Trim Pydantic's verbose error list to something the agent can
    read out loud without losing actionable detail."""
    return [
        {"loc": ".".join(str(x) for x in err["loc"]), "msg": err["msg"]}
        for err in e.errors()[:8]
    ]


__all__ = ["html_export_tool"]
