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


def test_opus_selection_is_logged(caplog) -> None:  # type: ignore[no-untyped-def]
    """The log line is structured so operator can audit Opus spend."""
    import logging
    caplog.set_level(logging.INFO)
    route_model(TaskType.MAX, caller="unit_test")
    # structlog pipes through stdlib; the raw message string should
    # mention opus + the caller tag at least once in the captured log.
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "opus" in joined.lower() or OPUS in joined or "model_router_opus_selected" in joined
