"""Intermediate representation for PILK web designs.

Every page the web_design_agent emits is first modeled as a :class:`Page`
of nested :class:`Container`s holding :class:`Widget`s. The IR is
intentionally smaller than any real page builder's settings surface —
it has one opinionated way to say each thing, so the agent can't
stall on a choice between twelve margin fields.

Mobile-first is structural, not a convention. Every responsive knob is
a :class:`ResponsiveValue` whose ``mobile`` value is required; ``tablet``
and ``desktop`` are optional and inherit upward (desktop → tablet →
mobile). An agent literally cannot emit a desktop-only layout — it has
to declare the mobile fallback.

Image sources accept either a URL or a :class:`CanvaAssetSpec`. The HTML
exporter (PR A) refuses the spec form loudly; PR I adds the resolver
that generates the asset via Canva at export time.

Design goals (in rank order):

1. One opinionated field per concept (no ``padding_top`` + ``padding_y``
   + ``padding``).
2. Validation catches the common screw-ups (missing alt text, invalid
   heading level, empty link) at IR construction, before the converter
   runs.
3. Shape stable enough that the HTML exporter and a future Elementor
   converter agent consume the same object without adapters.
"""

from __future__ import annotations

from typing import Annotated, Generic, Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


# ── Responsive wrapper ────────────────────────────────────────────


class ResponsiveValue(BaseModel, Generic[T]):
    """Mobile-first triple: mobile (required) + optional tablet/desktop.

    Inherit-upward semantics:

        desktop → tablet → mobile

    The HTML exporter uses this to emit Tailwind breakpoint prefixes
    only where the value actually differs from the next-smaller tier,
    which keeps the class list short and readable.
    """

    model_config = ConfigDict(extra="forbid")

    mobile: T
    tablet: T | None = None
    desktop: T | None = None

    def resolved(self) -> tuple[T, T, T]:
        """Return (mobile, tablet, desktop) with upward-inheritance applied."""
        tab = self.tablet if self.tablet is not None else self.mobile
        desk = self.desktop if self.desktop is not None else tab
        return (self.mobile, tab, desk)


# ── Spacing shape used on both sides + padding ───────────────────


class Spacing(BaseModel):
    """Four-sided CSS-style spacing in pixels. Scalar shorthand is
    intentionally absent — be explicit."""

    model_config = ConfigDict(extra="forbid")

    top: int = 0
    right: int = 0
    bottom: int = 0
    left: int = 0

    @classmethod
    def all(cls, n: int) -> Spacing:
        return cls(top=n, right=n, bottom=n, left=n)

    @classmethod
    def xy(cls, x: int, y: int) -> Spacing:
        return cls(top=y, right=x, bottom=y, left=x)


# ── Canva placeholder (resolved in PR I) ─────────────────────────


class CanvaAssetSpec(BaseModel):
    """Declares "generate an image via Canva at export time."

    In PR A the exporter errors if it encounters one; PR I adds the
    resolver that calls Canva, exports the PNG, and substitutes a URL
    before the bundle is written.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=3, max_length=500)
    design_type: str = Field(min_length=1, max_length=80)
    brand_kit_id: str | None = None


ImageSource = str | CanvaAssetSpec


# ── Widgets ───────────────────────────────────────────────────────


class _WidgetBase(BaseModel):
    """Base for the discriminated union. Don't instantiate directly."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex[:7])


class Heading(_WidgetBase):
    widget_type: Literal["heading"] = "heading"
    text: str = Field(min_length=1, max_length=500)
    level: Literal["h1", "h2", "h3", "h4", "h5", "h6"] = "h2"
    align: ResponsiveValue[Literal["left", "center", "right"]] | None = None


class Text(_WidgetBase):
    widget_type: Literal["text"] = "text"
    body: str = Field(min_length=1)
    align: ResponsiveValue[Literal["left", "center", "right"]] | None = None


class Button(_WidgetBase):
    widget_type: Literal["button"] = "button"
    label: str = Field(min_length=1, max_length=120)
    link: str = Field(min_length=1, max_length=2048)
    variant: Literal["primary", "secondary", "ghost"] = "primary"
    open_in_new_tab: bool = False


class Image(_WidgetBase):
    widget_type: Literal["image"] = "image"
    src: ImageSource
    alt: str = Field(min_length=1, max_length=400)
    caption: str | None = None

    @field_validator("alt")
    @classmethod
    def _alt_not_whitespace(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("alt text cannot be blank — accessibility-required")
        return v


class Spacer(_WidgetBase):
    widget_type: Literal["spacer"] = "spacer"
    height: ResponsiveValue[int]  # px


class Divider(_WidgetBase):
    widget_type: Literal["divider"] = "divider"
    color: str = "#e5e7eb"  # tailwind gray-200 default
    thickness_px: int = 1


class Icon(_WidgetBase):
    """Named icon from the Lucide set. Kept declarative so the exporter
    can pick its own rendering strategy (inline SVG, CDN <img>, etc.)."""

    widget_type: Literal["icon"] = "icon"
    name: str = Field(min_length=1, max_length=60)
    size_px: int = 24
    color: str = "currentColor"


class FormField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=120)
    type: Literal["text", "email", "tel", "textarea"] = "text"
    required: bool = False


class Form(_WidgetBase):
    widget_type: Literal["form"] = "form"
    action: str = Field(min_length=1, max_length=2048)
    method: Literal["POST", "GET"] = "POST"
    fields: list[FormField]
    submit_label: str = "Submit"

    @field_validator("fields")
    @classmethod
    def _at_least_one(cls, v: list[FormField]) -> list[FormField]:
        if not v:
            raise ValueError("form requires at least one field")
        return v


class Video(_WidgetBase):
    widget_type: Literal["video"] = "video"
    src: str = Field(min_length=1, max_length=2048)
    autoplay: bool = False
    controls: bool = True
    muted: bool = False


class HTMLEmbed(_WidgetBase):
    """Escape hatch for raw HTML. The exporter pastes this through
    verbatim — use sparingly and only for trusted content."""

    widget_type: Literal["html_embed"] = "html_embed"
    html: str = Field(min_length=1)


Widget = Annotated[
    Heading | Text | Button | Image | Spacer | Divider | Icon | Form | Video | HTMLEmbed,
    Field(discriminator="widget_type"),
]


# ── Container ─────────────────────────────────────────────────────


FlexDir = Literal["row", "column", "row-reverse", "column-reverse"]
JustifyContent = Literal["start", "center", "end", "between", "around"]
AlignItems = Literal["start", "center", "end", "stretch"]


class ContainerSettings(BaseModel):
    """Box-model + flex settings. Mobile-first defaults live in
    :mod:`core.tools.builtin.design.defaults`; this class holds only
    the shape, not the policy."""

    model_config = ConfigDict(extra="forbid")

    flex_direction: ResponsiveValue[FlexDir]
    gap: ResponsiveValue[int]  # px
    padding: ResponsiveValue[Spacing]
    margin: ResponsiveValue[Spacing]
    align_items: ResponsiveValue[AlignItems]
    justify_content: ResponsiveValue[JustifyContent]
    max_width: ResponsiveValue[int | None]  # px; None = full width
    background_color: str | None = None  # hex


class Container(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex[:7])
    settings: ContainerSettings
    children: list[Container | Widget] = Field(default_factory=list)


# PEP 604 with self-referential type: rebuild after class def so children
# resolves the forward reference cleanly.
Container.model_rebuild()


# ── Page ──────────────────────────────────────────────────────────


class Page(BaseModel):
    """Top-level IR object. A single page's worth of content.

    ``slug`` is kilometre-markers for exporters that emit routing info
    (e.g. Elementor / WordPress). The HTML exporter uses ``title`` for
    ``<title>``; ``slug`` is unused there but preserved so callers don't
    have to recompute it for a follow-up export.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=200)
    containers: list[Container] = Field(default_factory=list)
    # Language code used for the <html lang="..."> attribute.
    lang: str = "en"


__all__ = [
    "AlignItems",
    "Button",
    "CanvaAssetSpec",
    "Container",
    "ContainerSettings",
    "Divider",
    "FlexDir",
    "Form",
    "FormField",
    "HTMLEmbed",
    "Heading",
    "Icon",
    "Image",
    "ImageSource",
    "JustifyContent",
    "Page",
    "ResponsiveValue",
    "Spacer",
    "Spacing",
    "Text",
    "Video",
    "Widget",
]
