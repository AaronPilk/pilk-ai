"""elementor_validate tool-wrapper tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.policy.risk import RiskClass
from core.tools.builtin.design.elementor_validate import (
    elementor_validate_tool,
)
from core.tools.registry import ToolContext


def _valid_doc() -> dict:
    return {
        "version": "0.4",
        "title": "demo",
        "type": "page",
        "content": [
            {
                "id": "abc1234",
                "elType": "container",
                "settings": {},
                "elements": [
                    {
                        "id": "def5678",
                        "elType": "widget",
                        "widgetType": "heading",
                        "settings": {},
                        "elements": [],
                    }
                ],
                "isInner": False,
            }
        ],
        "page_settings": [],
    }


def test_risk_class_is_read() -> None:
    assert elementor_validate_tool.risk == RiskClass.READ


@pytest.mark.asyncio
async def test_inline_valid_document() -> None:
    out = await elementor_validate_tool.handler(
        {"document": _valid_doc()}, ToolContext()
    )
    assert not out.is_error
    assert out.data["valid"] is True
    assert out.data["element_counts"] == {"container": 1, "widget": 1}


@pytest.mark.asyncio
async def test_inline_invalid_document() -> None:
    bad = _valid_doc()
    bad["content"][0]["id"] = "NOTHEX"
    out = await elementor_validate_tool.handler(
        {"document": bad}, ToolContext()
    )
    assert out.is_error
    assert out.data["valid"] is False
    assert out.data["errors"]


@pytest.mark.asyncio
async def test_requires_document_or_path() -> None:
    out = await elementor_validate_tool.handler({}, ToolContext())
    assert out.is_error
    assert "document" in out.content and "path" in out.content


@pytest.mark.asyncio
async def test_document_must_be_object() -> None:
    out = await elementor_validate_tool.handler(
        {"document": "not a dict"}, ToolContext()
    )
    assert out.is_error
    assert "object" in out.content


@pytest.mark.asyncio
async def test_path_load_happy(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_text(json.dumps(_valid_doc()))
    out = await elementor_validate_tool.handler(
        {"path": str(path)}, ToolContext()
    )
    assert not out.is_error
    assert out.data["valid"] is True


@pytest.mark.asyncio
async def test_path_missing_surfaces_error(tmp_path: Path) -> None:
    out = await elementor_validate_tool.handler(
        {"path": str(tmp_path / "nope.json")}, ToolContext()
    )
    assert out.is_error
    assert "not found" in out.content


@pytest.mark.asyncio
async def test_path_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{ not json")
    out = await elementor_validate_tool.handler(
        {"path": str(path)}, ToolContext()
    )
    assert out.is_error
    assert "not valid JSON" in out.content


@pytest.mark.asyncio
async def test_path_non_object_top_level(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text(json.dumps([1, 2, 3]))
    out = await elementor_validate_tool.handler(
        {"path": str(path)}, ToolContext()
    )
    assert out.is_error
    assert "object" in out.content


@pytest.mark.asyncio
async def test_inline_beats_path(tmp_path: Path) -> None:
    """If both are provided, the inline dict wins — matches how
    agents commonly pass a freshly-drafted JSON without writing
    to disk first."""
    path = tmp_path / "wrong.json"
    path.write_text(json.dumps({"garbage": True}))
    out = await elementor_validate_tool.handler(
        {"document": _valid_doc(), "path": str(path)},
        ToolContext(),
    )
    assert not out.is_error
    assert out.data["valid"] is True


@pytest.mark.asyncio
async def test_returns_warnings_in_data() -> None:
    doc = _valid_doc()
    # Force a nested-container-not-inner warning.
    doc["content"][0]["elements"] = [
        {
            "id": "aaa0001",
            "elType": "container",
            "settings": {},
            "elements": [],
            "isInner": False,  # wrong for a nested container
        }
    ]
    out = await elementor_validate_tool.handler(
        {"document": doc}, ToolContext()
    )
    assert not out.is_error
    assert out.data["valid"] is True
    assert any(
        w["kind"] == "nested_container_not_inner"
        for w in out.data["warnings"]
    )


@pytest.mark.asyncio
async def test_error_content_summarizes_count() -> None:
    bad = _valid_doc()
    del bad["version"]
    out = await elementor_validate_tool.handler(
        {"document": bad}, ToolContext()
    )
    assert out.is_error
    assert "invalid" in out.content.lower()
    assert "error" in out.content.lower()
