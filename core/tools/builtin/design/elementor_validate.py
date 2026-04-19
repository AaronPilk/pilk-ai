"""PILK tool wrapper around :func:`validate_document`.

The ``elementor_converter_agent`` is expected to call this in a loop:

* Generate a draft Elementor JSON (by reasoning over the design IR).
* ``elementor_validate`` the draft — get structural errors + soft
  warnings.
* Patch and re-validate until it passes.
* Only then write the JSON to disk (``fs_write``) so
  ``wordpress_push`` can ship it.

The tool accepts either an inline dict or a path to a JSON file on
disk. Writing is not this tool's job — the agent does that with
``fs_write`` after a clean validation pass.

RiskClass: :data:`RiskClass.READ`. The tool only reads; no file
writes, no network, no approvals queue.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.policy.risk import RiskClass
from core.tools.builtin.design.elementor_schema import validate_document
from core.tools.registry import Tool, ToolContext, ToolOutcome


async def _handler(args: dict, ctx: ToolContext) -> ToolOutcome:
    # Inline dict takes precedence over path, matching "hand me raw
    # JSON and validate it" being the common LLM flow.
    inline = args.get("document")
    path = str(args.get("path") or "").strip()

    if inline is not None:
        if not isinstance(inline, dict):
            return ToolOutcome(
                content=(
                    "elementor_validate: 'document' must be a JSON object "
                    "(the top-level Elementor template export)."
                ),
                is_error=True,
            )
        doc = inline
    elif path:
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ToolOutcome(
                content=f"elementor_validate: path not found: {path}",
                is_error=True,
            )
        except json.JSONDecodeError as e:
            return ToolOutcome(
                content=f"elementor_validate: {path} is not valid JSON: {e}",
                is_error=True,
            )
        except OSError as e:
            return ToolOutcome(
                content=f"elementor_validate: cannot read {path}: {e}",
                is_error=True,
            )
        if not isinstance(doc, dict):
            return ToolOutcome(
                content=(
                    f"elementor_validate: {path} top-level must be a JSON "
                    "object, not an array or scalar."
                ),
                is_error=True,
            )
    else:
        return ToolOutcome(
            content=(
                "elementor_validate requires either 'document' (inline "
                "dict) or 'path' (file path)."
            ),
            is_error=True,
        )

    result = validate_document(doc)
    data: dict[str, Any] = {
        "valid": result.valid,
        "errors": result.errors,
        "warnings": [w.model_dump() for w in result.warnings],
        "element_counts": result.element_counts,
        "max_depth_seen": result.max_depth_seen,
    }
    if result.valid:
        containers = result.element_counts.get("container", 0)
        widgets = result.element_counts.get("widget", 0)
        warn_count = len(result.warnings)
        content = (
            f"valid Elementor document "
            f"({containers} container(s), {widgets} widget(s), "
            f"depth {result.max_depth_seen}"
            f"{f', {warn_count} warning(s)' if warn_count else ''})."
        )
        return ToolOutcome(content=content, data=data)

    content = f"invalid Elementor document — {len(result.errors)} error(s)."
    return ToolOutcome(content=content, data=data, is_error=True)


elementor_validate_tool = Tool(
    name="elementor_validate",
    description=(
        "Validate an Elementor template-export JSON against the schema "
        "the WordPress Elementor plugin expects. Accepts either "
        "'document' (inline JSON object) or 'path' (file path). Returns "
        "structured errors + soft warnings without raising — the "
        "converter agent loops on this to self-correct. RiskClass.READ "
        "— no side effects."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "document": {
                "type": "object",
                "description": (
                    "Inline Elementor JSON. Takes precedence over 'path' "
                    "if both are supplied."
                ),
            },
            "path": {
                "type": "string",
                "description": "Absolute path to an Elementor JSON file.",
            },
        },
    },
    risk=RiskClass.READ,
    handler=_handler,
)


__all__ = ["elementor_validate_tool"]
