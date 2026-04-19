"""Mobile-first defaults for the design IR.

Factories here build ``ResponsiveValue``s with sensible fallbacks so
agents don't need to explicitly pick a mobile value for every knob.
They're imported lazily by the HTML exporter when a container's
setting is missing — the IR itself never autofills; that's the
converter's job.

The values reflect what a reasonable mobile-first landing page looks
like, not what a kitchen-sink CMS dumps into its default theme.
"""

from __future__ import annotations

from core.tools.builtin.design.ir import (
    AlignItems,
    ContainerSettings,
    FlexDir,
    JustifyContent,
    ResponsiveValue,
    Spacing,
)

# Tailwind's defaults (matches Elementor's tablet/desktop thresholds).
BREAKPOINT_TABLET_PX: int = 768
BREAKPOINT_DESKTOP_PX: int = 1024

# Reasonable "section" container — vertical stack on mobile, row on
# desktop, comfortable padding, centered content.
DEFAULT_SECTION_PADDING = Spacing(top=48, right=20, bottom=48, left=20)
DEFAULT_SECTION_PADDING_DESKTOP = Spacing(top=96, right=32, bottom=96, left=32)


def default_section_settings() -> ContainerSettings:
    """Top-level section container — mobile column → desktop row."""
    return ContainerSettings(
        flex_direction=ResponsiveValue[FlexDir](mobile="column", desktop="row"),
        gap=ResponsiveValue[int](mobile=16, desktop=32),
        padding=ResponsiveValue[Spacing](
            mobile=DEFAULT_SECTION_PADDING,
            desktop=DEFAULT_SECTION_PADDING_DESKTOP,
        ),
        margin=ResponsiveValue[Spacing](mobile=Spacing()),
        align_items=ResponsiveValue[AlignItems](mobile="stretch", desktop="center"),
        justify_content=ResponsiveValue[JustifyContent](mobile="start", desktop="center"),
        max_width=ResponsiveValue[int | None](mobile=None, desktop=1200),
        background_color=None,
    )


def default_inner_settings() -> ContainerSettings:
    """Inner container — vertical stack everywhere, tight padding,
    no max-width cap (inherits parent's)."""
    return ContainerSettings(
        flex_direction=ResponsiveValue[FlexDir](mobile="column"),
        gap=ResponsiveValue[int](mobile=8),
        padding=ResponsiveValue[Spacing](mobile=Spacing()),
        margin=ResponsiveValue[Spacing](mobile=Spacing()),
        align_items=ResponsiveValue[AlignItems](mobile="start"),
        justify_content=ResponsiveValue[JustifyContent](mobile="start"),
        max_width=ResponsiveValue[int | None](mobile=None),
        background_color=None,
    )


__all__ = [
    "BREAKPOINT_DESKTOP_PX",
    "BREAKPOINT_TABLET_PX",
    "DEFAULT_SECTION_PADDING",
    "DEFAULT_SECTION_PADDING_DESKTOP",
    "default_inner_settings",
    "default_section_settings",
]
