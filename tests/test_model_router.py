"""model_router — task-type → model selection."""
from __future__ import annotations

from core.utils.model_router import (
    HAIKU,
    OPUS,
    SONNET,
    TaskType,
    route_model,
)


def test_haiku_for_cheap_tasks() -> None:
    for task in (
        TaskType.CLASSIFY,
        TaskType.EXTRACT,
        TaskType.TAG,
        TaskType.SCORE,
        TaskType.SUMMARIZE_SHORT,
    ):
        assert route_model(task) == HAIKU, task


def test_sonnet_for_medium_tasks() -> None:
    for task in (
        TaskType.DRAFT,
        TaskType.EMAIL,
        TaskType.COPY,
        TaskType.REASON,
        TaskType.STRATEGY,
        TaskType.SUMMARIZE_LONG,
    ):
        assert route_model(task) == SONNET, task


def test_opus_for_max_only() -> None:
    assert route_model(TaskType.MAX) == OPUS


def test_raw_string_is_accepted() -> None:
    """Manifests store task types as YAML strings."""
    assert route_model("classify") == HAIKU
    assert route_model("COPY") == SONNET  # case-insensitive
    assert route_model("  draft  ") == SONNET  # whitespace tolerated


def test_unknown_falls_back_to_haiku() -> None:
    """Default cheap — a typo never silently escalates to Opus."""
    assert route_model("nonsense_task") == HAIKU
    assert route_model(None) == HAIKU
    assert route_model("") == HAIKU


def test_opus_selection_is_logged(capsys) -> None:  # type: ignore[no-untyped-def]
    """The log line is structured so operator can audit Opus spend.

    structlog is wired to stdout (not stdlib logging) on this project,
    so we read captured stdout rather than caplog.
    """
    route_model(TaskType.MAX, caller="unit_test")
    captured = capsys.readouterr().out
    assert "model_router_opus_selected" in captured
    assert "unit_test" in captured
    assert OPUS in captured
