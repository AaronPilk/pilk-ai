# `core/tools/builtin/design/`

Design IR + exporters for the PILK `web_design_agent` (and, eventually,
the Elementor-converter agent that reads the same IR).

## Scope of this PR (PR A)

- `ir.py` — Pydantic models for pages, containers, widgets, responsive
  values, spacing, and the Canva asset placeholder.
- `defaults.py` — mobile-first factory helpers for container settings.
- `html.py` — pure-function `ir_to_html(page) -> {"index.html", "styles.css"}`.
- `html_export.py` — the `html_export` PILK tool (`RiskClass.READ`).

**Not in this PR:**
- Elementor conversion. A separate LLM-driven agent handles that in a
  follow-up PR; this IR is its input.
- Canva asset resolution. `CanvaAssetSpec` is defined but the HTML
  exporter raises `CanvaUnresolvedError` when it encounters one.
  PR I adds the resolver.

## IR at a glance

```yaml
title: Acme Launch
slug: acme-launch
lang: en
containers:
  - settings:                    # ContainerSettings
      flex_direction: {mobile: column, desktop: row}
      gap: {mobile: 16, desktop: 32}
      padding:
        mobile: {top: 48, right: 20, bottom: 48, left: 20}
        desktop: {top: 96, right: 32, bottom: 96, left: 32}
      margin: {mobile: {top: 0, right: 0, bottom: 0, left: 0}}
      align_items: {mobile: center}
      justify_content: {mobile: center}
      max_width: {mobile: null, desktop: 1200}
      background_color: "#f8fafc"   # optional
    children:
      - widget_type: heading
        text: Launch better, faster.
        level: h1
        align: {mobile: center}
      - widget_type: text
        body: Skyway builds landing pages that convert.
      - widget_type: button
        label: Start now
        link: "#start"
        variant: primary
```

Every `Responsive*` field takes `{mobile, tablet?, desktop?}`. Values
inherit upward: `desktop` falls back to `tablet`, `tablet` falls back
to `mobile`. Mobile is required — you can't ship a desktop-only layout
by accident.

## Widget catalogue

| Widget | Key fields |
|---|---|
| `heading` | `text`, `level` (h1–h6), `align?` |
| `text` | `body`, `align?` |
| `button` | `label`, `link`, `variant` (primary/secondary/ghost), `open_in_new_tab?` |
| `image` | `src` (URL or `CanvaAssetSpec`), `alt` (required, non-empty), `caption?` |
| `spacer` | `height` (responsive px) |
| `divider` | `color`, `thickness_px` |
| `icon` | `name` (Lucide-style), `size_px`, `color` |
| `form` | `action`, `method`, `fields[]`, `submit_label` |
| `video` | `src`, `autoplay`, `controls`, `muted` |
| `html_embed` | `html` (raw passthrough, escape hatch) |

## Mobile-first breakpoints

Matches Tailwind + Elementor defaults:

- **mobile** — < 768px (no prefix)
- **tablet** — ≥ 768px (`md:` prefix)
- **desktop** — ≥ 1024px (`lg:` prefix)

The exporter only emits a breakpoint class when the value actually
differs from the next-smaller tier, so class lists stay diffable.

## Tool invocation

```python
await html_export_tool.handler(
    {
        "ir": page_dict,          # validated via Pydantic
        "output_dir": "/abs/out", # created if missing
    },
    ctx,
)
```

Returns:

- Success: two files written, `ToolOutcome.data = {output_dir, files,
  container_count, title, slug}`.
- Validation error: `is_error=True`, first 8 Pydantic errors in `data`.
- Canva spec encountered: `is_error=True` with `"PR I"` in the message.
- Write failure (ENOSPC, perms, etc.): `is_error=True` with the OSError text.

## Adding a new widget

1. Define a Pydantic model in `ir.py` with `widget_type: Literal["..."]`.
2. Add it to the `Widget` discriminated-union alias.
3. Write a `_render_x(w)` renderer in `html.py`, adding the `match`
   branch in `_render_widget`.
4. Add a fixture under `tests/fixtures/design/` that uses it.
5. Add a structural assertion in `tests/test_html_converter.py`.

The converter stays a single pure function; no new filesystem
plumbing is required.
