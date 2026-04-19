"""Pydantic IR validation tests — no converter, no fixtures loaded."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.tools.builtin.design.ir import (
    Button,
    CanvaAssetSpec,
    Container,
    ContainerSettings,
    Heading,
    Image,
    Page,
    ResponsiveValue,
    Spacing,
)

# ── ResponsiveValue ──────────────────────────────────────────────


def test_responsive_mobile_required() -> None:
    with pytest.raises(ValidationError):
        ResponsiveValue[int].model_validate({"desktop": 16})


def test_responsive_desktop_inherits_tablet_inherits_mobile() -> None:
    v = ResponsiveValue[int](mobile=10)
    assert v.resolved() == (10, 10, 10)


def test_responsive_tablet_overrides_mobile_only() -> None:
    v = ResponsiveValue[int](mobile=10, tablet=20)
    assert v.resolved() == (10, 20, 20)


def test_responsive_desktop_overrides_independently() -> None:
    v = ResponsiveValue[int](mobile=10, desktop=30)
    assert v.resolved() == (10, 10, 30)


# ── Widget validation ────────────────────────────────────────────


def test_heading_level_must_be_valid() -> None:
    with pytest.raises(ValidationError):
        Heading(text="hi", level="h7")


def test_heading_text_required_nonempty() -> None:
    with pytest.raises(ValidationError):
        Heading(text="", level="h1")


def test_image_alt_required() -> None:
    with pytest.raises(ValidationError):
        Image(src="https://x.com/a.png", alt="")


def test_image_alt_whitespace_only_rejected() -> None:
    with pytest.raises(ValidationError, match="accessibility"):
        Image(src="https://x.com/a.png", alt="   ")


def test_button_link_nonempty() -> None:
    with pytest.raises(ValidationError):
        Button(label="Click", link="")


def test_widget_ids_auto_generate() -> None:
    h1 = Heading(text="a")
    h2 = Heading(text="b")
    assert h1.id != h2.id
    assert len(h1.id) == 7


def test_canva_asset_spec_carries_through() -> None:
    # Image with Canva spec source validates at the IR layer.
    # Resolution/rejection happens later (HTML in PR A, resolver in PR I).
    img = Image(
        src=CanvaAssetSpec(query="lighthouse at dawn", design_type="hero_image"),
        alt="Lighthouse hero image",
    )
    assert isinstance(img.src, CanvaAssetSpec)


# ── Container / Page ─────────────────────────────────────────────


def _minimal_settings() -> ContainerSettings:
    return ContainerSettings(
        flex_direction=ResponsiveValue(mobile="column"),
        gap=ResponsiveValue[int](mobile=0),
        padding=ResponsiveValue[Spacing](mobile=Spacing()),
        margin=ResponsiveValue[Spacing](mobile=Spacing()),
        align_items=ResponsiveValue(mobile="start"),
        justify_content=ResponsiveValue(mobile="start"),
        max_width=ResponsiveValue[int | None](mobile=None),
    )


def test_container_nests_containers() -> None:
    inner = Container(settings=_minimal_settings(), children=[Heading(text="h")])
    outer = Container(settings=_minimal_settings(), children=[inner])
    assert isinstance(outer.children[0], Container)


def test_container_mixed_children() -> None:
    c = Container(
        settings=_minimal_settings(),
        children=[Heading(text="h"), Container(settings=_minimal_settings())],
    )
    assert len(c.children) == 2


def test_page_requires_title_and_slug() -> None:
    with pytest.raises(ValidationError):
        Page(title="", slug="x")
    with pytest.raises(ValidationError):
        Page(title="x", slug="")


def test_id_uniqueness_across_a_large_tree() -> None:
    """50 auto-generated IDs collide zero times under uuid4[:7]."""
    seen: set[str] = set()
    for _ in range(50):
        for h in [Heading(text="a") for _ in range(10)]:
            assert h.id not in seen
            seen.add(h.id)


# ── Spacing helpers ──────────────────────────────────────────────


def test_spacing_all() -> None:
    s = Spacing.all(12)
    assert (s.top, s.right, s.bottom, s.left) == (12, 12, 12, 12)


def test_spacing_xy() -> None:
    s = Spacing.xy(16, 8)
    assert (s.top, s.right, s.bottom, s.left) == (8, 16, 8, 16)


# ── Extra-field rejection ────────────────────────────────────────


def test_heading_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match="extra"):
        Heading.model_validate({"text": "a", "level": "h1", "oops": 1})
