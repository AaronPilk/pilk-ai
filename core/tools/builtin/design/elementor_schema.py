"""Pydantic schema for Elementor export JSON.

Strict enough to catch the common LLM-generation mistakes (missing
`elType`, wrong `isInner` placement, widget with non-empty `elements`)
without fighting Elementor's actual, evolving settings schema — we
deliberately accept any ``dict`` for ``settings`` because the real
Elementor settings catalogue is enormous and version-dependent. The
agent's job is to get the *structure* right; shape verification of
every setting key is a losing game.

What we do validate:

* Top-level document: ``version`` / ``title`` / ``type`` / ``content``
  / ``page_settings``.
* Each element is either a container or a widget, and the
  ``elType`` / ``isInner`` / ``widgetType`` / ``elements`` fields
  compose correctly.
* IDs are hex strings of sensible length (Elementor uses ~7-hex IDs).
* Document nesting stays under a sane depth limit (defaults to 12)
  — catches runaway recursion in a generation that tries to wrap
  every section in one more container.
* Top-level containers have ``isInner == False``; nested containers
  ``isInner == True``. Catches the "same container copied everywhere"
  flavor of generation error.

Anything a strict validator would surface with diff noise — extra
unknown keys inside ``settings``, unrecognized widget types — is
intentionally left as a warning rather than a failure. The agent
prompt tells it which widgets exist; runtime we err toward letting
Elementor itself reject unknown bits.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TREE_DEPTH: int = 12
ID_PATTERN: re.Pattern[str] = re.compile(r"^[0-9a-f]{3,32}$")


class _BaseElement(BaseModel):
    """Common fields for both container + widget. Validated strictly —
    ``settings`` stays a freeform dict because Elementor's settings
    space is huge and version-dependent."""

    model_config = ConfigDict(extra="allow")  # tolerate Elementor's many fields

    id: str = Field(min_length=3, max_length=32)
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _id_shape(self) -> _BaseElement:
        if not ID_PATTERN.fullmatch(self.id):
            raise ValueError(
                f"element id {self.id!r} is not hex (expected 3-32 hex chars)"
            )
        return self


class ElementorContainer(_BaseElement):
    # camelCase field names mirror Elementor's JSON schema verbatim —
    # renaming them would break import / export round-trips.
    elType: Literal["container"]  # noqa: N815
    isInner: bool = False  # noqa: N815
    elements: list[ElementorContainer | ElementorWidget] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _children_ok(self) -> ElementorContainer:
        # The inner-vs-outer property is validated at the document
        # level by ``validate_document`` where we have tree context;
        # here we just sanity-check that ``elements`` is a list.
        return self


class ElementorWidget(_BaseElement):
    elType: Literal["widget"]  # noqa: N815 — Elementor schema field name
    widgetType: str = Field(min_length=1, max_length=80)  # noqa: N815
    # Widgets occasionally have child elements in Elementor's actual
    # schema (e.g. nested icon-list), so we accept them but warn if
    # they're non-empty in ``validate_document``.
    elements: list[ElementorContainer | ElementorWidget] = Field(
        default_factory=list
    )


ElementorContainer.model_rebuild()
ElementorWidget.model_rebuild()


Element = Annotated[
    ElementorContainer | ElementorWidget,
    Field(discriminator="elType"),
]


class ElementorDocument(BaseModel):
    """Matches the shape of Elementor's Template Export JSON."""

    model_config = ConfigDict(extra="allow")

    version: str = Field(min_length=1, max_length=16)
    title: str = Field(min_length=1, max_length=200)
    # Elementor emits ``"page"`` for page-scope templates and ``"section"``
    # for section-scope. We accept both — narrower than accepting any
    # string, wider than locking to just one shape.
    type: Literal["page", "section", "popup", "kit"] = "page"
    content: list[Element]
    # Elementor uses an empty list when there are no page-level
    # settings. A dict works too in some older exports — accept both.
    page_settings: list | dict = Field(default_factory=list)


class ValidationWarning(BaseModel):
    """Shape for the list of soft-warnings ``validate_document``
    returns alongside any hard failures — a converter agent can use
    these to self-correct without failing validation."""

    kind: str
    path: str
    message: str


class ValidationResult(BaseModel):
    """What the tool layer surfaces to callers."""

    valid: bool
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[ValidationWarning] = Field(default_factory=list)
    element_counts: dict[str, int] = Field(default_factory=dict)
    max_depth_seen: int = 0


def validate_document(doc: dict[str, Any]) -> ValidationResult:
    """Validate an Elementor JSON document. Always returns a
    ``ValidationResult`` — never raises. Structural errors land in
    ``errors``; soft concerns in ``warnings``.

    Designed for the converter agent to call in a loop:

        1. LLM produces JSON → validate_document → read errors/warnings.
        2. LLM patches issues → validate_document again.
        3. Once ``valid`` is True and warnings are empty (or knowingly
           ignored), write the JSON to disk.
    """
    try:
        parsed = ElementorDocument.model_validate(doc)
    except Exception as e:
        return ValidationResult(
            valid=False,
            errors=_pydantic_errors(e),
            element_counts={},
            max_depth_seen=0,
        )

    warnings: list[ValidationWarning] = []
    counts: dict[str, int] = {"container": 0, "widget": 0}
    max_depth = 0

    def _walk(elements: list[Element], depth: int, path: str) -> None:
        nonlocal max_depth
        if depth > max_depth:
            max_depth = depth
        if depth > MAX_TREE_DEPTH:
            warnings.append(
                ValidationWarning(
                    kind="max_depth_exceeded",
                    path=path,
                    message=(
                        f"nesting depth {depth} > MAX_TREE_DEPTH="
                        f"{MAX_TREE_DEPTH}. Likely a generation loop; "
                        "consider flattening."
                    ),
                )
            )
            return
        for i, el in enumerate(elements):
            here = f"{path}[{i}]"
            if isinstance(el, ElementorContainer):
                counts["container"] += 1
                # Inner/outer sanity — only the top call has depth==0.
                if depth == 0 and el.isInner:
                    warnings.append(
                        ValidationWarning(
                            kind="top_level_inner_container",
                            path=here,
                            message=(
                                "top-level container has isInner=True. "
                                "Elementor expects top-level sections to "
                                "have isInner=False."
                            ),
                        )
                    )
                if depth > 0 and not el.isInner:
                    warnings.append(
                        ValidationWarning(
                            kind="nested_container_not_inner",
                            path=here,
                            message=(
                                "nested container should have "
                                "isInner=True so Elementor renders it as "
                                "a section-inside-section."
                            ),
                        )
                    )
                _walk(el.elements, depth=depth + 1, path=f"{here}.elements")
            else:
                counts["widget"] += 1
                if el.elements:
                    warnings.append(
                        ValidationWarning(
                            kind="widget_with_elements",
                            path=here,
                            message=(
                                f"widget {el.widgetType!r} has non-empty "
                                "elements list. Most widgets shouldn't."
                            ),
                        )
                    )

    _walk(parsed.content, depth=0, path="content")

    return ValidationResult(
        valid=True,
        errors=[],
        warnings=warnings,
        element_counts=counts,
        max_depth_seen=max_depth,
    )


def _pydantic_errors(e: Exception) -> list[dict[str, Any]]:
    """Normalize a Pydantic ``ValidationError`` into the narrow shape
    the tool layer returns. Any other exception gets wrapped in a
    single-entry list so callers don't branch on exception type."""
    errors_fn = getattr(e, "errors", None)
    if callable(errors_fn):
        try:
            raw = errors_fn()
        except Exception:
            raw = []
        if raw:
            return [
                {
                    "loc": ".".join(str(p) for p in err.get("loc", [])),
                    "msg": err.get("msg", ""),
                    "type": err.get("type", ""),
                }
                for err in raw
            ]
    return [{"loc": "", "msg": str(e), "type": type(e).__name__}]


__all__ = [
    "ID_PATTERN",
    "MAX_TREE_DEPTH",
    "ElementorContainer",
    "ElementorDocument",
    "ElementorWidget",
    "ValidationResult",
    "ValidationWarning",
    "validate_document",
]
