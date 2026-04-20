"""Renderer + tool tests for the print-design surface.

The Playwright PDF export path is mocked — spinning up Chromium in
every test would be slow + flaky on CI. HTML generation is pure
Python so we exercise it directly: shape of the output, escape
safety, size catalogue correctness, workspace-scope enforcement.

The validator is exercised with a fixture PDF byte string we write
to tmp_path; no real PDF needed to verify the validator reads the
header + MediaBox correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.integrations.print_design import (
    PRODUCT_SIZES,
    render_banner_html,
    render_business_card_html,
    render_flyer_html,
    validate_pdf_basics,
)
from core.policy.risk import RiskClass
from core.tools.builtin.print_design import (
    PRINT_DESIGN_TOOLS,
    print_design_banner_tool,
    print_design_business_card_tool,
    print_design_flyer_tool,
    print_design_list_templates_tool,
)
from core.tools.registry import ToolContext


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(sandbox_root=tmp_path)


# ── size catalogue ──────────────────────────────────────────────


def test_catalogue_covers_core_products() -> None:
    for expected in (
        "US_LETTER",
        "A4",
        "BUSINESS_CARD_US",
        "ROLLUP_33X80",
        "TRADESHOW_BACKDROP_8X10",
        "POSTER_24X36",
    ):
        assert expected in PRODUCT_SIZES


def test_every_product_has_positive_bleed() -> None:
    """No zero-bleed templates — even posters get crop marks because
    commercial shops require them."""
    for s in PRODUCT_SIZES.values():
        assert s.bleed_in > 0, s.slug


# ── registry shape ──────────────────────────────────────────────


def test_tool_count_is_four() -> None:
    assert len(PRINT_DESIGN_TOOLS) == 4


def test_render_tools_are_write_local() -> None:
    for t in PRINT_DESIGN_TOOLS:
        if t.name in {
            "print_design_flyer",
            "print_design_business_card",
            "print_design_banner",
        }:
            assert t.risk == RiskClass.WRITE_LOCAL, t.name


def test_list_templates_is_read() -> None:
    assert print_design_list_templates_tool.risk == RiskClass.READ


# ── HTML generation ─────────────────────────────────────────────


def test_flyer_html_embeds_title_and_page_size() -> None:
    html = render_flyer_html(size_slug="US_LETTER", title="Open House")
    assert "Open House" in html
    assert "@page" in html
    assert "8.75in 11.25in" in html
    assert "bleed: 0.125in" in html
    assert "marks: crop cross" in html


def test_flyer_html_escapes_user_content() -> None:
    """A title containing HTML should end up escaped — otherwise the
    first operator who drops an ampersand blows up the document."""
    html = render_flyer_html(
        size_slug="US_LETTER",
        title="<script>alert('xss')</script>",
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_flyer_rejects_malicious_brand_color() -> None:
    """A brand_color with a `</style>` escape would let the user
    break out of the <style> block. Unrecognised values fall back to
    the default, silently."""
    html = render_flyer_html(
        size_slug="US_LETTER",
        title="Test",
        brand_color="#111; </style><script>",
    )
    assert "</style><script>" not in html


def test_business_card_html_embeds_all_fields() -> None:
    html = render_business_card_html(
        name="Aaron Pilk",
        title="Founder",
        company="Skyway Media",
        phone="555-1234",
        email="aaron@skyway.media",
        website="skyway.media",
    )
    for needle in (
        "Aaron Pilk",
        "Founder",
        "Skyway Media",
        "555-1234",
        "aaron@skyway.media",
        "skyway.media",
    ):
        assert needle in html


def test_banner_scales_type_by_height() -> None:
    """A taller banner should emit a larger headline px value than
    a shorter one — keeps the type readable at the physical distance
    the banner is viewed from."""
    poster = render_banner_html(
        size_slug="POSTER_24X36", headline="BIG",
    )
    backdrop = render_banner_html(
        size_slug="TRADESHOW_BACKDROP_8X10", headline="BIG",
    )

    def _headline_px(html: str) -> int:
        import re as _re
        m = _re.search(r"\.headline[^}]*font-size:\s*(\d+)px", html)
        assert m is not None
        return int(m.group(1))

    assert _headline_px(backdrop) > _headline_px(poster)


# ── validator ───────────────────────────────────────────────────


def test_validator_reads_pdf_header_and_mediabox(tmp_path: Path) -> None:
    # Synthesize a minimal byte stream that starts with the PDF
    # magic and includes a parseable MediaBox.
    fake = b"%PDF-1.7\n/MediaBox [ 0 0 612 792 ]\n%%EOF"
    path = tmp_path / "fake.pdf"
    path.write_bytes(fake)
    v = validate_pdf_basics(path)
    assert v["ok"] is True
    assert v["width_in"] == 8.5
    assert v["height_in"] == 11.0
    assert v["pdf_version"] == "1.7"


def test_validator_rejects_non_pdf(tmp_path: Path) -> None:
    path = tmp_path / "bogus.pdf"
    path.write_bytes(b"not a pdf")
    v = validate_pdf_basics(path)
    assert v["ok"] is False
    assert "%PDF-" in v["error"]


def test_validator_reports_missing_file(tmp_path: Path) -> None:
    v = validate_pdf_basics(tmp_path / "nope.pdf")
    assert v["ok"] is False


# ── tool arg validation (no PDF export required) ───────────────


@pytest.mark.asyncio
async def test_flyer_requires_title(ctx: ToolContext) -> None:
    out = await print_design_flyer_tool.handler({}, ctx)
    assert out.is_error
    assert "title" in out.content


@pytest.mark.asyncio
async def test_flyer_rejects_unknown_size(ctx: ToolContext) -> None:
    out = await print_design_flyer_tool.handler(
        {"title": "x", "size": "TABLOID"}, ctx,
    )
    assert out.is_error
    assert "TABLOID" in out.content


@pytest.mark.asyncio
async def test_business_card_requires_name(ctx: ToolContext) -> None:
    out = await print_design_business_card_tool.handler({}, ctx)
    assert out.is_error


@pytest.mark.asyncio
async def test_banner_rejects_unknown_size(ctx: ToolContext) -> None:
    out = await print_design_banner_tool.handler(
        {"headline": "x", "size": "BILLBOARD_48FT"}, ctx,
    )
    assert out.is_error


@pytest.mark.asyncio
async def test_list_templates_lists_every_product(
    ctx: ToolContext,
) -> None:
    out = await print_design_list_templates_tool.handler({}, ctx)
    assert not out.is_error
    slugs = {t["slug"] for t in out.data["templates"]}
    assert slugs == set(PRODUCT_SIZES.keys())


# ── tool with mocked PDF export (workspace + error flow) ───────


class _StubExport:
    """Stand-in for export_pdf. Writes a minimal valid PDF so the
    validator still sees a real file + MediaBox, but never touches
    Chromium."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    async def __call__(self, html: str, size, out_path: Path) -> Path:  # type: ignore[no-untyped-def]
        self.calls.append((html[:80], out_path))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Emit a valid-looking PDF header with a MediaBox matching the
        # requested product size — the validator reports those points
        # back to the caller so a stub that lies would mask bugs.
        width_pts = round(size.width_in * 72)
        height_pts = round(size.height_in * 72)
        out_path.write_bytes(
            b"%PDF-1.7\n"
            + f"/MediaBox [ 0 0 {width_pts} {height_pts} ]\n".encode()
            + b"%%EOF"
        )
        return out_path


@pytest.mark.asyncio
async def test_flyer_happy_path_writes_html_and_pdf(
    ctx: ToolContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubExport()
    from core.tools.builtin import print_design as mod
    monkeypatch.setattr(mod, "export_pdf", stub)
    out = await print_design_flyer_tool.handler(
        {
            "title": "Open House",
            "subtitle": "Saturday @ 10 AM",
            "cta_text": "RSVP",
            "cta_url": "https://example.com",
        },
        ctx,
    )
    assert not out.is_error
    assert out.data["pdf_path"].startswith("print/flyer/")
    assert out.data["html_path"].startswith("print/flyer/")
    assert (tmp_path / out.data["html_path"]).is_file()
    assert (tmp_path / out.data["pdf_path"]).is_file()
    # The validator-reported dimensions match US_LETTER spec.
    v = out.data["validation"]
    assert v["ok"] is True
    assert v["width_in"] == 8.75
    assert v["height_in"] == 11.25


@pytest.mark.asyncio
async def test_flyer_export_failure_still_writes_html(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Chromium dies, we should still have the HTML on disk so
    the operator can print-to-PDF manually. The tool surfaces the
    error but doesn't throw away the work."""

    async def _boom(*_a, **_kw) -> None:
        raise RuntimeError("chromium launch failed")

    from core.tools.builtin import print_design as mod
    monkeypatch.setattr(mod, "export_pdf", _boom)
    out = await print_design_flyer_tool.handler({"title": "Open House"}, ctx)
    assert out.is_error
    assert "HTML wrote" in out.content
    assert out.data["html_path"].startswith("print/flyer/")


@pytest.mark.asyncio
async def test_flyer_rejects_hero_escaping_workspace(
    ctx: ToolContext,
) -> None:
    out = await print_design_flyer_tool.handler(
        {"title": "x", "hero_image": "../../etc/passwd"}, ctx,
    )
    assert out.is_error
    assert "escapes workspace" in out.content
