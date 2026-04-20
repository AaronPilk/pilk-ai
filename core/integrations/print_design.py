"""Print-design HTML renderer + Playwright PDF exporter.

Generates print-ready HTML layouts for common marketing collateral —
flyers, business cards, rollup banners, trade-show backdrops — with
proper bleed and crop-mark CSS, then runs Playwright's chromium PDF
exporter over the HTML to produce press-ready PDFs at 300 DPI.

Design choices worth flagging:

* **Chromium page.pdf() is the renderer.** We already depend on
  Playwright for the browser tool; reusing the same runtime avoids a
  second PDF library. We drive it with ``prefer_css_page_size=True`` +
  an ``@page`` block, which is the idiomatic way to get bleed + crop
  marks out of Chromium.

* **RGB, not CMYK.** Generating true press-CMYK without ghostscript
  or ReportLab is a rabbit hole. Every modern print shop (MOO,
  Vistaprint, UPrinting, Jukebox, PrintingForLess) accepts RGB PDFs
  at 300 DPI and does the RGB→CMYK conversion internally with
  profiles tuned to their paper stock. V1 ships RGB with a note in
  the tool output telling the operator to request the shop's ICC
  profile only if color-critical (brand reds, logo oranges).

* **Templates are inline strings, not Jinja.** The layouts are simple
  enough that Python f-strings + careful HTML-escape are clearer
  than pulling in a templating dep and managing template paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from core.logging import get_logger

log = get_logger("pilkd.print_design")

# Inches. All specs include bleed — the trim size is (W - 2·bleed) x
# (H - 2·bleed). Standard 0.125in bleed for small items; 0.25in for
# anything over 24in; 0.5in for trade-show large-format.


@dataclass(frozen=True)
class ProductSize:
    slug: str
    label: str
    width_in: float
    height_in: float
    bleed_in: float
    orientation: str  # "portrait" | "landscape"
    notes: str = ""


PRODUCT_SIZES: dict[str, ProductSize] = {
    # Flyers
    "US_LETTER": ProductSize(
        slug="US_LETTER",
        label="US Letter flyer (8.5x11)",
        width_in=8.75,
        height_in=11.25,
        bleed_in=0.125,
        orientation="portrait",
        notes="Standard US flyer. Trim 8.5x11. Most print shops accept 300 DPI RGB.",
    ),
    "A4": ProductSize(
        slug="A4",
        label="A4 flyer (210x297mm)",
        width_in=8.517,  # 216.36mm (210 + 2x3mm bleed)
        height_in=11.929,
        bleed_in=0.118,
        orientation="portrait",
        notes="Standard A4 with 3mm bleed. EU-default.",
    ),
    # Business cards (US standard — 3.5x2)
    "BUSINESS_CARD_US": ProductSize(
        slug="BUSINESS_CARD_US",
        label="Business card — US (3.5x2)",
        width_in=3.75,
        height_in=2.25,
        bleed_in=0.125,
        orientation="landscape",
        notes="Standard US business card. Trim 3.5x2. Safe area 3.25x1.75.",
    ),
    # Banners
    "ROLLUP_33X80": ProductSize(
        slug="ROLLUP_33X80",
        label="Rollup banner (33x80)",
        width_in=33.5,
        height_in=80.5,
        bleed_in=0.25,
        orientation="portrait",
        notes=(
            "Standard retractable rollup. The bottom 6-8in is usually "
            "inside the base cartridge — keep critical art above that."
        ),
    ),
    "TRADESHOW_BACKDROP_8X10": ProductSize(
        slug="TRADESHOW_BACKDROP_8X10",
        label="Trade-show backdrop (8ft x 10ft)",
        width_in=120.5,
        height_in=96.5,
        bleed_in=0.5,
        orientation="landscape",
        notes=(
            "Pop-up tension-fabric backdrop. 8ft tall x 10ft wide "
            "with seams every 30-36in depending on printer. Ask the "
            "shop for their exact seam placement and avoid putting "
            "faces across a seam."
        ),
    ),
    "POSTER_24X36": ProductSize(
        slug="POSTER_24X36",
        label="Poster (24x36)",
        width_in=24.5,
        height_in=36.5,
        bleed_in=0.25,
        orientation="portrait",
        notes="Standard event poster.",
    ),
}


DEFAULT_BRAND = "#111827"
DEFAULT_ACCENT = "#2563eb"
DEFAULT_BG = "#ffffff"
DEFAULT_FG = "#111827"
# Conservative safe-area inset relative to the trimmed edge — most
# modern shops want ≥0.25in inside the trim; we go 0.375in to stay
# clear of low-end equipment variance.
SAFE_AREA_INSET_IN = 0.375


def page_css(size: ProductSize) -> str:
    """Emit the ``@page`` + base-reset CSS for a given product size.
    Uses ``size:`` with bleed + crop marks — the browser's PDF backend
    honours this when exported with ``prefer_css_page_size=True``."""
    return (
        f"""
        @page {{
          size: {size.width_in}in {size.height_in}in;
          margin: 0;
          marks: crop cross;
          bleed: {size.bleed_in}in;
        }}
        html, body {{
          margin: 0;
          padding: 0;
          -webkit-print-color-adjust: exact;
          print-color-adjust: exact;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                       "Helvetica Neue", Arial, sans-serif;
          font-size: 16px;
          color: {DEFAULT_FG};
          background: {DEFAULT_BG};
        }}
        .page {{
          width: {size.width_in}in;
          height: {size.height_in}in;
          position: relative;
          overflow: hidden;
          box-sizing: border-box;
        }}
        .safe {{
          position: absolute;
          top: {size.bleed_in + SAFE_AREA_INSET_IN}in;
          right: {size.bleed_in + SAFE_AREA_INSET_IN}in;
          bottom: {size.bleed_in + SAFE_AREA_INSET_IN}in;
          left: {size.bleed_in + SAFE_AREA_INSET_IN}in;
          display: flex;
          flex-direction: column;
        }}
        """
    )


def _color(raw: str | None, fallback: str) -> str:
    """Very light sanitisation — take only valid CSS hex/rgb strings.
    We don't try to be a parser; anything that isn't obvious gets
    rejected in favor of the fallback so stray ``</style>`` can never
    leak into the template body."""
    if not raw:
        return fallback
    v = raw.strip()
    if v.startswith("#") and all(c.isalnum() for c in v[1:]) and len(v) in {4, 7, 9}:
        return v
    if v.startswith("rgb(") and v.endswith(")") and "<" not in v:
        return v
    return fallback


def _img_data_uri(path: Path) -> str | None:
    """Inline an image as a data URI — Chromium's PDF exporter fetches
    by URL, and we don't always have a local file:// that resolves
    cleanly. Data URIs sidestep that."""
    import base64
    import mimetypes
    if not path.is_file():
        return None
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


# ── Flyer template ───────────────────────────────────────────────


def render_flyer_html(
    *,
    size_slug: str,
    title: str,
    subtitle: str = "",
    body: str = "",
    cta_text: str = "",
    cta_url: str = "",
    brand_color: str | None = None,
    accent_color: str | None = None,
    hero_image: Path | None = None,
) -> str:
    size = PRODUCT_SIZES[size_slug]
    brand = _color(brand_color, DEFAULT_BRAND)
    accent = _color(accent_color, DEFAULT_ACCENT)
    hero_uri = _img_data_uri(hero_image) if hero_image else None
    hero_html = (
        f'<div class="hero" style="background-image: url({hero_uri!r});"></div>'
        if hero_uri
        else ""
    )
    cta_html = (
        f'<a class="cta" href="{escape(cta_url, quote=True)}">'
        f"{escape(cta_text)}</a>"
        if cta_text
        else ""
    )
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><style>
{page_css(size)}
.page.flyer {{ background: {DEFAULT_BG}; }}
.flyer .hero {{
  flex: 1 1 auto;
  min-height: 45%;
  background-size: cover;
  background-position: center;
  border-radius: 8px;
  margin-bottom: 0.5in;
}}
.flyer .title {{
  color: {brand};
  font-size: 56px;
  font-weight: 800;
  line-height: 1.05;
  letter-spacing: -0.02em;
}}
.flyer .subtitle {{
  margin-top: 0.15in;
  font-size: 24px;
  color: #4b5563;
}}
.flyer .body {{
  margin-top: 0.3in;
  font-size: 16px;
  line-height: 1.5;
  color: #374151;
  white-space: pre-line;
}}
.flyer .cta {{
  align-self: flex-start;
  margin-top: auto;
  padding: 14px 24px;
  background: {accent};
  color: white;
  font-weight: 700;
  font-size: 20px;
  border-radius: 6px;
  text-decoration: none;
}}
</style></head>
<body>
  <div class="page flyer">
    <div class="safe">
      {hero_html}
      <div class="title">{escape(title)}</div>
      {f'<div class="subtitle">{escape(subtitle)}</div>' if subtitle else ''}
      {f'<div class="body">{escape(body)}</div>' if body else ''}
      {cta_html}
    </div>
  </div>
</body>
</html>"""


# ── Business card template ──────────────────────────────────────


def render_business_card_html(
    *,
    name: str,
    title: str = "",
    company: str = "",
    phone: str = "",
    email: str = "",
    website: str = "",
    brand_color: str | None = None,
    accent_color: str | None = None,
    logo_image: Path | None = None,
) -> str:
    size = PRODUCT_SIZES["BUSINESS_CARD_US"]
    brand = _color(brand_color, DEFAULT_BRAND)
    accent = _color(accent_color, DEFAULT_ACCENT)
    logo_uri = _img_data_uri(logo_image) if logo_image else None
    logo_html = (
        f'<img class="logo" src="{escape(logo_uri, quote=True)}" />'
        if logo_uri
        else ""
    )
    contact_lines = [
        (phone, "tel"),
        (email, "email"),
        (website, "web"),
    ]
    contact_html = "".join(
        f'<div class="contact-line contact-{kind}">{escape(v)}</div>'
        for v, kind in contact_lines
        if v
    )
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><style>
{page_css(size)}
.page.card {{
  background: {DEFAULT_BG};
  border-top: 0.15in solid {accent};
}}
.card .row {{
  display: flex;
  align-items: flex-start;
  gap: 0.2in;
  height: 100%;
}}
.card .logo {{
  width: 0.7in;
  height: 0.7in;
  object-fit: contain;
}}
.card .text {{
  flex: 1;
  display: flex;
  flex-direction: column;
}}
.card .name {{
  color: {brand};
  font-size: 18px;
  font-weight: 800;
  letter-spacing: -0.01em;
}}
.card .title {{
  color: #4b5563;
  font-size: 11px;
  margin-top: 2px;
}}
.card .company {{
  color: {accent};
  font-size: 11px;
  font-weight: 600;
  margin-top: 2px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}
.card .contact-line {{
  font-size: 9px;
  color: #374151;
  margin-top: 2px;
}}
.card .text > .contact-line:first-of-type {{ margin-top: auto; }}
</style></head>
<body>
  <div class="page card">
    <div class="safe">
      <div class="row">
        {logo_html}
        <div class="text">
          <div class="name">{escape(name)}</div>
          {f'<div class="title">{escape(title)}</div>' if title else ''}
          {f'<div class="company">{escape(company)}</div>' if company else ''}
          {contact_html}
        </div>
      </div>
    </div>
  </div>
</body>
</html>"""


# ── Banner template (rollup, backdrop, poster) ──────────────────


def render_banner_html(
    *,
    size_slug: str,
    headline: str,
    subhead: str = "",
    body: str = "",
    cta_text: str = "",
    cta_url: str = "",
    brand_color: str | None = None,
    accent_color: str | None = None,
    hero_image: Path | None = None,
) -> str:
    size = PRODUCT_SIZES[size_slug]
    brand = _color(brand_color, DEFAULT_BRAND)
    accent = _color(accent_color, DEFAULT_ACCENT)
    hero_uri = _img_data_uri(hero_image) if hero_image else None
    # Headline and body scale with the physical size — a 33x80 rollup
    # wants bigger type than a 24x36 poster.
    scale = size.height_in / 11.0
    headline_px = int(64 * scale)
    subhead_px = int(24 * scale)
    body_px = int(16 * scale)
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><style>
{page_css(size)}
.page.banner {{
  background: {DEFAULT_BG};
}}
.banner .hero {{
  height: 45%;
  background-image: url({f"'{hero_uri}'" if hero_uri else "''"});
  background-size: cover;
  background-position: center;
  border-radius: {0.2 * scale}in;
  margin-bottom: 0.5in;
}}
.banner .headline {{
  color: {brand};
  font-size: {headline_px}px;
  font-weight: 900;
  line-height: 1.02;
  letter-spacing: -0.02em;
}}
.banner .subhead {{
  color: #4b5563;
  font-size: {subhead_px}px;
  margin-top: 0.2in;
}}
.banner .body {{
  color: #374151;
  font-size: {body_px}px;
  line-height: 1.5;
  margin-top: 0.4in;
  white-space: pre-line;
}}
.banner .cta {{
  align-self: flex-start;
  margin-top: auto;
  padding: {0.15 * scale}in {0.3 * scale}in;
  background: {accent};
  color: white;
  font-weight: 800;
  font-size: {subhead_px}px;
  border-radius: 8px;
  text-decoration: none;
}}
</style></head>
<body>
  <div class="page banner">
    <div class="safe">
      {'<div class="hero"></div>' if hero_uri else ''}
      <div class="headline">{escape(headline)}</div>
      {f'<div class="subhead">{escape(subhead)}</div>' if subhead else ''}
      {f'<div class="body">{escape(body)}</div>' if body else ''}
      {f'<a class="cta" href="{escape(cta_url, quote=True)}">{escape(cta_text)}</a>' if cta_text else ''}
    </div>
  </div>
</body>
</html>"""


# ── PDF export via Playwright ───────────────────────────────────


async def export_pdf(html: str, size: ProductSize, out_path: Path) -> Path:
    """Render ``html`` via headless Chromium and write a print-ready
    PDF to ``out_path``. Uses ``prefer_css_page_size=True`` so the
    ``@page`` block in our templates controls size + bleed + crop
    marks — those are the three things a press operator actually
    checks on hand-off."""
    from playwright.async_api import async_playwright

    out_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="domcontentloaded")
            await page.pdf(
                path=str(out_path),
                prefer_css_page_size=True,
                print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            )
        finally:
            await browser.close()
    return out_path


def validate_pdf_basics(path: Path) -> dict[str, Any]:
    """Cheap local validation — no external ghostscript required.
    Checks the file exists, has the %PDF- header, reports size in
    bytes, and finds the declared page dimensions via a regex over
    the content stream. Enough to catch 'the render died silently'
    without pretending we're doing full-on preflight."""
    if not path.is_file():
        return {"ok": False, "error": f"not found: {path}"}
    raw = path.read_bytes()
    if not raw.startswith(b"%PDF-"):
        return {
            "ok": False,
            "error": "file does not start with %PDF- header",
            "size_bytes": len(raw),
        }
    # Extract the first /MediaBox value if present — tells the
    # operator the rendered trim size in points (72 pt/in).
    import re as _re
    mb = _re.search(rb"/MediaBox\s*\[([^\]]+)\]", raw)
    media_box: list[float] | None = None
    if mb:
        try:
            media_box = [float(x) for x in mb.group(1).split()[:4]]
        except ValueError:
            media_box = None
    width_in = height_in = None
    if media_box and len(media_box) == 4:
        width_in = round((media_box[2] - media_box[0]) / 72.0, 3)
        height_in = round((media_box[3] - media_box[1]) / 72.0, 3)
    return {
        "ok": True,
        "size_bytes": len(raw),
        "media_box_pts": media_box,
        "width_in": width_in,
        "height_in": height_in,
        "pdf_version": raw[5:8].decode("ascii", errors="replace"),
    }


__all__ = [
    "PRODUCT_SIZES",
    "ProductSize",
    "export_pdf",
    "render_banner_html",
    "render_business_card_html",
    "render_flyer_html",
    "validate_pdf_basics",
]
