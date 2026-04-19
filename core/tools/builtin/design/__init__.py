"""Design subsystem — IR + exporters.

Public surface:

    ir          — Pydantic models (Page, Container, Widget, ...).
    defaults    — mobile-first factory helpers for ContainerSettings.
    html        — ir_to_html(page) pure converter.
    html_export — PILK tool wrapper for the web_design_agent.

Future work lives in sibling modules:
* ``elementor.py`` is **not** part of this package in PR A — an
  Elementor-converter agent (LLM-driven) lands in a follow-up PR.
* Canva asset resolution (``canva_resolver.py``) lands in PR I.
"""

from core.tools.builtin.design.html import CanvaUnresolvedError, ir_to_html
from core.tools.builtin.design.html_export import html_export_tool
from core.tools.builtin.design.ir import (
    Button,
    CanvaAssetSpec,
    Container,
    ContainerSettings,
    Divider,
    Form,
    FormField,
    Heading,
    HTMLEmbed,
    Icon,
    Image,
    Page,
    ResponsiveValue,
    Spacer,
    Spacing,
    Text,
    Video,
)
from core.tools.builtin.design.wordpress_push import wordpress_push_tool

__all__ = [
    "Button",
    "CanvaAssetSpec",
    "CanvaUnresolvedError",
    "Container",
    "ContainerSettings",
    "Divider",
    "Form",
    "FormField",
    "HTMLEmbed",
    "Heading",
    "Icon",
    "Image",
    "Page",
    "ResponsiveValue",
    "Spacer",
    "Spacing",
    "Text",
    "Video",
    "html_export_tool",
    "ir_to_html",
    "wordpress_push_tool",
]
