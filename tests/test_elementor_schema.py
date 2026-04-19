"""Elementor JSON schema + validate_document tests.

Covers the happy path + every class of structural error the
converter agent is expected to avoid / self-correct.
"""

from __future__ import annotations

from core.tools.builtin.design.elementor_schema import (
    MAX_TREE_DEPTH,
    validate_document,
)

# ── Valid-document happy path ──────────────────────────────────


def _minimal_doc(content: list | None = None) -> dict:
    return {
        "version": "0.4",
        "title": "demo",
        "type": "page",
        "content": content if content is not None else [],
        "page_settings": [],
    }


def _container(id_: str, children=None, is_inner: bool = False) -> dict:
    return {
        "id": id_,
        "elType": "container",
        "settings": {},
        "elements": children or [],
        "isInner": is_inner,
    }


def _widget(id_: str, widget_type: str = "heading") -> dict:
    return {
        "id": id_,
        "elType": "widget",
        "widgetType": widget_type,
        "settings": {},
        "elements": [],
    }


def test_empty_content_is_valid() -> None:
    result = validate_document(_minimal_doc())
    assert result.valid
    assert result.errors == []
    assert result.element_counts == {"container": 0, "widget": 0}


def test_single_container_with_heading_valid() -> None:
    doc = _minimal_doc([_container("abc1234", [_widget("def5678")])])
    result = validate_document(doc)
    assert result.valid
    assert result.element_counts == {"container": 1, "widget": 1}


# ── Document-shape errors ──────────────────────────────────────


def test_missing_version() -> None:
    doc = _minimal_doc()
    del doc["version"]
    result = validate_document(doc)
    assert not result.valid
    assert any("version" in e["loc"] for e in result.errors)


def test_missing_content() -> None:
    doc = _minimal_doc()
    del doc["content"]
    result = validate_document(doc)
    assert not result.valid
    assert any("content" in e["loc"] for e in result.errors)


def test_unknown_type_rejected() -> None:
    doc = _minimal_doc()
    doc["type"] = "gallery"  # not in the Literal
    result = validate_document(doc)
    assert not result.valid


# ── Element-shape errors ───────────────────────────────────────


def test_element_without_eltype() -> None:
    bad = {
        "id": "abc1234",
        "settings": {},
        "elements": [],
        "isInner": False,
    }
    result = validate_document(_minimal_doc([bad]))
    assert not result.valid


def test_widget_without_widget_type() -> None:
    bad = {
        "id": "abc1234",
        "elType": "widget",
        "settings": {},
        "elements": [],
    }
    result = validate_document(_minimal_doc([bad]))
    assert not result.valid


def test_id_must_be_hex() -> None:
    doc = _minimal_doc([_container("NOT-HEX")])
    result = validate_document(doc)
    assert not result.valid
    assert any("hex" in e["msg"].lower() for e in result.errors)


def test_id_too_short_rejected() -> None:
    doc = _minimal_doc([_container("ab")])
    result = validate_document(doc)
    assert not result.valid


# ── Soft warnings ──────────────────────────────────────────────


def test_top_level_container_with_inner_true_warns() -> None:
    doc = _minimal_doc([_container("abc1234", is_inner=True)])
    result = validate_document(doc)
    assert result.valid  # structural ok — warning only
    assert any(
        w.kind == "top_level_inner_container" for w in result.warnings
    )


def test_nested_container_missing_inner_true_warns() -> None:
    inner = _container("def5678", is_inner=False)  # should be True
    outer = _container("abc1234", [inner], is_inner=False)
    result = validate_document(_minimal_doc([outer]))
    assert result.valid
    assert any(
        w.kind == "nested_container_not_inner" for w in result.warnings
    )


def test_widget_with_nonempty_elements_warns() -> None:
    widget = _widget("abc1234")
    widget["elements"] = [_widget("def5678")]
    result = validate_document(_minimal_doc([widget]))
    assert result.valid
    assert any(
        w.kind == "widget_with_elements" for w in result.warnings
    )


def test_excess_depth_warns_once() -> None:
    """Build a deeper-than-MAX_TREE_DEPTH tree; validation stays
    valid but a single depth warning fires."""
    # MAX+1 levels of nesting.
    doc_inner: dict | None = None
    for i in range(MAX_TREE_DEPTH + 2):
        next_container = _container(
            f"{i:07x}",
            [doc_inner] if doc_inner is not None else [],
            is_inner=i > 0,
        )
        doc_inner = next_container
    result = validate_document(_minimal_doc([doc_inner]))
    assert result.valid
    assert any(
        w.kind == "max_depth_exceeded" for w in result.warnings
    )


# ── Element counts + max depth ─────────────────────────────────


def test_element_counts_correctly_tallied() -> None:
    doc = _minimal_doc(
        [
            _container(
                "aaa0001",
                [
                    _widget("bbb0002"),
                    _widget("bbb0003"),
                    _container(
                        "ccc0004",
                        [_widget("bbb0005")],
                        is_inner=True,
                    ),
                ],
            )
        ]
    )
    result = validate_document(doc)
    assert result.valid
    assert result.element_counts == {"container": 2, "widget": 3}


def test_max_depth_seen_reports_actual_depth() -> None:
    doc = _minimal_doc(
        [
            _container(
                "aaa0001",
                [
                    _container(
                        "bbb0002",
                        [_widget("ccc0003")],
                        is_inner=True,
                    )
                ],
            )
        ]
    )
    # depth: content(0) → container(1) → nested container(2) → widget lives under 2.
    result = validate_document(doc)
    assert result.max_depth_seen >= 2


# ── Non-dict defense ───────────────────────────────────────────


def test_validate_non_dict_returns_error() -> None:
    result = validate_document("not a dict")  # type: ignore[arg-type]
    assert not result.valid
    assert result.errors


def test_validate_document_never_raises() -> None:
    """Any input shape produces a ValidationResult — callers never
    have to wrap this in try/except."""
    for sample in [None, 42, "string", [], {"random": "keys"}, {"version": 1}]:
        result = validate_document(sample)  # type: ignore[arg-type]
        assert result.valid in (True, False)
