"""Task-type → model router.

Routes agent LLM calls to the cheapest model capable of handling the
task. Motivation: Opus is ~15x the price of Haiku and burns through
Anthropic's per-org ITPM cap. Plenty of agent work (tagging, scoring,
one-shot extraction, short summaries) runs fine on Haiku. Default to
Haiku and upgrade only on explicit signal.

Usage::

    from core.utils.model_router import route_model, TaskType

    model = route_model(TaskType.TAG)   # → "claude-haiku-4-5"
    model = route_model("copy")          # → "claude-sonnet-4-6"
    model = route_model("max")           # → "claude-opus-4-7" (logged)

Unknown task types fall back to Haiku. Every Opus selection is logged
with the caller's task type so the operator can audit spend.
"""

from __future__ import annotations

from enum import StrEnum

from core.logging import get_logger

log = get_logger("pilkd.model_router")


# Model constants. Keep aligned with settings.py tier_*_model defaults.
HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-7"


class TaskType(StrEnum):
    """Canonical task categories the router understands.

    :class:`StrEnum` so callers can also pass raw string values (handy
    for manifest-driven agents whose YAML stores a plain string).
    """

    # Cheap + fast — Haiku
    CLASSIFY = "classify"
    EXTRACT = "extract"
    TAG = "tag"
    SCORE = "score"
    SUMMARIZE_SHORT = "summarize_short"

    # Medium — Sonnet
    DRAFT = "draft"
    EMAIL = "email"
    COPY = "copy"
    REASON = "reason"
    STRATEGY = "strategy"
    SUMMARIZE_LONG = "summarize_long"

    # Heavy — Opus (explicit opt-in only)
    MAX = "max"


# Mapping from task type to model. Anything not listed defaults to
# HAIKU so a typo never silently escalates to Opus.
_ROUTING: dict[TaskType, str] = {
    TaskType.CLASSIFY: HAIKU,
    TaskType.EXTRACT: HAIKU,
    TaskType.TAG: HAIKU,
    TaskType.SCORE: HAIKU,
    TaskType.SUMMARIZE_SHORT: HAIKU,
    TaskType.DRAFT: SONNET,
    TaskType.EMAIL: SONNET,
    TaskType.COPY: SONNET,
    TaskType.REASON: SONNET,
    TaskType.STRATEGY: SONNET,
    TaskType.SUMMARIZE_LONG: SONNET,
    TaskType.MAX: OPUS,
}


def route_model(task_type: TaskType | str | None, *, caller: str = "unknown") -> str:
    """Return the model id for ``task_type``.

    ``caller`` is an optional free-text tag used only for the Opus
    selection log line — helps attribute unexpected Opus spend when
    you go looking.

    Unknown types (``None``, typos, unmapped enum values) fall back
    to HAIKU. That keeps the default cheap; an agent has to explicitly
    opt into something bigger.
    """
    if task_type is None:
        return HAIKU
    # Accept raw strings so manifest YAML can hand us a bare key.
    if isinstance(task_type, str) and not isinstance(task_type, TaskType):
        try:
            task_type = TaskType(task_type.lower().strip())
        except ValueError:
            log.debug(
                "model_router_unknown_task",
                task_type=task_type,
                caller=caller,
                fallback=HAIKU,
            )
            return HAIKU
    model = _ROUTING.get(task_type, HAIKU)
    if model == OPUS:
        log.info(
            "model_router_opus_selected",
            task_type=task_type.value,
            caller=caller,
            model=model,
        )
    return model


__all__ = [
    "HAIKU",
    "OPUS",
    "SONNET",
    "TaskType",
    "route_model",
]
