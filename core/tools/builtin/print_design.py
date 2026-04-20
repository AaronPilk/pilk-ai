"""Print-design tools — print-ready PDFs for flyers, business cards,
and banners. Output lands in workspace/print/<kind>/<slug>.{html,pdf}
so downstream tools (email delivery, approvals) can attach the files.

Four tools cover the V1 surface:

    print_design_flyer            WRITE_LOCAL   US_LETTER | A4
    print_design_business_card    WRITE_LOCAL   US 3.5x2
    print_design_banner           WRITE_LOCAL   rollup / backdrop / poster
    print_design_list_templates   READ          size catalogue

``validate_pdf_basics`` runs on every output so the agent sees the
actual rendered dimensions — if Chromium silently drops the @page
block (has happened on older versions), the tool catches it rather
than letting a bad PDF ship to a print shop.

No new API keys. Renderer is pure Python + Playwright (already in the
dependency set for the browser tool). Images are inlined as data
URIs so the PDF is self-contained — the operator can hand it
straight to a shop without worrying about linked-asset resolution.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.config import get_settings
from core.integrations.print_design import (
    PRODUCT_SIZES,
    export_pdf,
    render_banner_html,
    render_business_card_html,
    render_flyer_html,
    validate_pdf_basics,
)
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.tools.print_design")


FLYER_SIZES = {"US_LETTER", "A4"}
BANNER_SIZES = {"ROLLUP_33X80", "TRADESHOW_BACKDROP_8X10", "POSTER_24X36"}

_SLUG_RE = re.compile(r"[^a-z0-9\-]+")


# ── helpers ──────────────────────────────────────────────────────


def _workspace_root(ctx: ToolContext) -> Path:
    return (
        ctx.sandbox_root.expanduser().resolve()
        if ctx.sandbox_root is not None
        else get_settings().workspace_dir.expanduser().resolve()
    )


def _resolve_in_workspace(ctx: ToolContext, rel: str) -> Path | None:
    """Resolve a workspace-relative path; None if it escapes the root.
    Callers that want an error ToolOutcome wrap the None themselves."""
    root = _workspace_root(ctx)
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _slugify(raw: str, fallback: str = "design") -> str:
    s = (raw or "").lower().strip().replace(" ", "-")
    s = _SLUG_RE.sub("-", s).strip("-")
    return s or fallback


def _out_paths(ctx: ToolContext, kind: str, slug: str) -> tuple[Path, Path]:
    """Return (html_path, pdf_path) under workspace/print/<kind>/."""
    root = _workspace_root(ctx)
    base = root / "print" / kind / slug
    return base.with_suffix(".html"), base.with_suffix(".pdf")


async def _write_and_export(
    html: str, size_slug: str, kind: str, slug: str, ctx: ToolContext,
) -> ToolOutcome:
    size = PRODUCT_SIZES[size_slug]
    html_path, pdf_path = _out_paths(ctx, kind, slug)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")
    try:
        await export_pdf(html, size, pdf_path)
    except Exception as e:
        log.exception("print_pdf_export_failed", slug=slug, kind=kind)
        return ToolOutcome(
            content=(
                f"HTML wrote to print/{kind}/{slug}.html but PDF "
                f"export failed: {type(e).__name__}: {e}. Open the "
                "HTML in Chrome and File → Print → Save as PDF as a "
                "manual fallback."
            ),
            is_error=True,
            data={"html_path": str(html_path.relative_to(_workspace_root(ctx)))},
        )
    valid = validate_pdf_basics(pdf_path)
    rel_pdf = pdf_path.relative_to(_workspace_root(ctx)).as_posix()
    rel_html = html_path.relative_to(_workspace_root(ctx)).as_posix()
    return ToolOutcome(
        content=(
            f"Wrote {kind} '{slug}' — {rel_pdf} "
            f"({valid.get('width_in')}x{valid.get('height_in')}in, "
            f"{round((valid.get('size_bytes') or 0) / 1024)}KB). "
            f"RGB at 300 DPI. Ask the print shop for their ICC "
            "profile only if color-critical."
        ),
        data={
            "pdf_path": rel_pdf,
            "html_path": rel_html,
            "size_slug": size_slug,
            "validation": valid,
        },
    )


# ── Flyer ────────────────────────────────────────────────────────


async def _flyer(args: dict, ctx: ToolContext) -> ToolOutcome:
    title = str(args.get("title") or "").strip()
    if not title:
        return ToolOutcome(
            content="print_design_flyer requires 'title'.",
            is_error=True,
        )
    size_slug = str(args.get("size") or "US_LETTER").upper()
    if size_slug not in FLYER_SIZES:
        return ToolOutcome(
            content=f"size must be one of {sorted(FLYER_SIZES)}; got {size_slug}",
            is_error=True,
        )
    hero_path: Path | None = None
    hero_rel = (args.get("hero_image") or "").strip()
    if hero_rel:
        resolved = _resolve_in_workspace(ctx, hero_rel)
        if resolved is None:
            return ToolOutcome(
                content=f"hero_image escapes workspace: {hero_rel}",
                is_error=True,
            )
        if not resolved.is_file():
            return ToolOutcome(
                content=f"hero_image not found: {hero_rel}. Ask "
                        "creative_content_agent to render one first.",
                is_error=True,
            )
        hero_path = resolved
    html = render_flyer_html(
        size_slug=size_slug,
        title=title,
        subtitle=str(args.get("subtitle") or ""),
        body=str(args.get("body") or ""),
        cta_text=str(args.get("cta_text") or ""),
        cta_url=str(args.get("cta_url") or ""),
        brand_color=args.get("brand_color"),
        accent_color=args.get("accent_color"),
        hero_image=hero_path,
    )
    slug = _slugify(args.get("slug") or title, "flyer")
    return await _write_and_export(html, size_slug, "flyer", slug, ctx)


print_design_flyer_tool = Tool(
    name="print_design_flyer",
    description=(
        "Generate a print-ready flyer PDF with proper bleed + crop "
        "marks. Size: US_LETTER (8.5x11) or A4. Writes both HTML "
        "(preview) and PDF (hand-off) to workspace/print/flyer/. "
        "hero_image is workspace-relative; have creative_content_"
        "agent render one first if needed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "subtitle": {"type": "string"},
            "body": {"type": "string"},
            "cta_text": {"type": "string"},
            "cta_url": {"type": "string"},
            "size": {"type": "string", "enum": sorted(FLYER_SIZES)},
            "hero_image": {
                "type": "string",
                "description": "Workspace-relative path to a PNG/JPG.",
            },
            "brand_color": {
                "type": "string",
                "description": "Hex like #111827 or rgb(...).",
            },
            "accent_color": {"type": "string"},
            "slug": {
                "type": "string",
                "description": "Override the filename stem. Defaults to slugified title.",
            },
        },
        "required": ["title"],
    },
    risk=RiskClass.WRITE_LOCAL,
    handler=_flyer,
)


# ── Business card ───────────────────────────────────────────────


async def _business_card(args: dict, ctx: ToolContext) -> ToolOutcome:
    name = str(args.get("name") or "").strip()
    if not name:
        return ToolOutcome(
            content="print_design_business_card requires 'name'.",
            is_error=True,
        )
    logo_path: Path | None = None
    logo_rel = (args.get("logo_image") or "").strip()
    if logo_rel:
        resolved = _resolve_in_workspace(ctx, logo_rel)
        if resolved is None:
            return ToolOutcome(
                content=f"logo_image escapes workspace: {logo_rel}",
                is_error=True,
            )
        if not resolved.is_file():
            return ToolOutcome(
                content=f"logo_image not found: {logo_rel}",
                is_error=True,
            )
        logo_path = resolved
    html = render_business_card_html(
        name=name,
        title=str(args.get("title") or ""),
        company=str(args.get("company") or ""),
        phone=str(args.get("phone") or ""),
        email=str(args.get("email") or ""),
        website=str(args.get("website") or ""),
        brand_color=args.get("brand_color"),
        accent_color=args.get("accent_color"),
        logo_image=logo_path,
    )
    slug = _slugify(args.get("slug") or name, "business-card")
    return await _write_and_export(
        html, "BUSINESS_CARD_US", "business-card", slug, ctx,
    )


print_design_business_card_tool = Tool(
    name="print_design_business_card",
    description=(
        "Generate a US standard (3.5x2) business card PDF with bleed "
        "+ crop marks. Logo is workspace-relative. Writes to "
        "workspace/print/business-card/."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "title": {"type": "string"},
            "company": {"type": "string"},
            "phone": {"type": "string"},
            "email": {"type": "string"},
            "website": {"type": "string"},
            "logo_image": {"type": "string"},
            "brand_color": {"type": "string"},
            "accent_color": {"type": "string"},
            "slug": {"type": "string"},
        },
        "required": ["name"],
    },
    risk=RiskClass.WRITE_LOCAL,
    handler=_business_card,
)


# ── Banner (rollup / backdrop / poster) ─────────────────────────


async def _banner(args: dict, ctx: ToolContext) -> ToolOutcome:
    headline = str(args.get("headline") or "").strip()
    if not headline:
        return ToolOutcome(
            content="print_design_banner requires 'headline'.",
            is_error=True,
        )
    size_slug = str(args.get("size") or "ROLLUP_33X80").upper()
    if size_slug not in BANNER_SIZES:
        return ToolOutcome(
            content=f"size must be one of {sorted(BANNER_SIZES)}; got {size_slug}",
            is_error=True,
        )
    hero_path: Path | None = None
    hero_rel = (args.get("hero_image") or "").strip()
    if hero_rel:
        resolved = _resolve_in_workspace(ctx, hero_rel)
        if resolved is None:
            return ToolOutcome(
                content=f"hero_image escapes workspace: {hero_rel}",
                is_error=True,
            )
        if not resolved.is_file():
            return ToolOutcome(
                content=f"hero_image not found: {hero_rel}",
                is_error=True,
            )
        hero_path = resolved
    html = render_banner_html(
        size_slug=size_slug,
        headline=headline,
        subhead=str(args.get("subhead") or ""),
        body=str(args.get("body") or ""),
        cta_text=str(args.get("cta_text") or ""),
        cta_url=str(args.get("cta_url") or ""),
        brand_color=args.get("brand_color"),
        accent_color=args.get("accent_color"),
        hero_image=hero_path,
    )
    slug = _slugify(args.get("slug") or headline, "banner")
    return await _write_and_export(html, size_slug, "banner", slug, ctx)


print_design_banner_tool = Tool(
    name="print_design_banner",
    description=(
        "Generate a large-format banner PDF — rollup (33x80), trade-"
        "show backdrop (8ft x 10ft), or poster (24x36). Headline and "
        "body type scale with the physical size. Writes to "
        "workspace/print/banner/."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "subhead": {"type": "string"},
            "body": {"type": "string"},
            "cta_text": {"type": "string"},
            "cta_url": {"type": "string"},
            "size": {"type": "string", "enum": sorted(BANNER_SIZES)},
            "hero_image": {"type": "string"},
            "brand_color": {"type": "string"},
            "accent_color": {"type": "string"},
            "slug": {"type": "string"},
        },
        "required": ["headline"],
    },
    risk=RiskClass.WRITE_LOCAL,
    handler=_banner,
)


# ── List templates ──────────────────────────────────────────────


async def _list_templates(_args: dict, _ctx: ToolContext) -> ToolOutcome:
    rows = [
        {
            "slug": s.slug,
            "label": s.label,
            "trim_in": [
                round(s.width_in - 2 * s.bleed_in, 3),
                round(s.height_in - 2 * s.bleed_in, 3),
            ],
            "bleed_in": s.bleed_in,
            "orientation": s.orientation,
            "notes": s.notes,
        }
        for s in PRODUCT_SIZES.values()
    ]
    lines = [
        f"- {r['slug']}: {r['label']} · trim {r['trim_in'][0]}x"
        f"{r['trim_in'][1]}in, bleed {r['bleed_in']}in"
        for r in rows
    ]
    return ToolOutcome(
        content="Available templates:\n" + "\n".join(lines),
        data={"templates": rows},
    )


print_design_list_templates_tool = Tool(
    name="print_design_list_templates",
    description=(
        "List every supported print product with its trim size, "
        "bleed, and orientation. Use this when the operator's brief "
        "is ambiguous about size — match their words to the right "
        "slug before calling a render tool."
    ),
    input_schema={"type": "object", "properties": {}},
    risk=RiskClass.READ,
    handler=_list_templates,
)


PRINT_DESIGN_TOOLS: list[Tool] = [
    print_design_flyer_tool,
    print_design_business_card_tool,
    print_design_banner_tool,
    print_design_list_templates_tool,
]
