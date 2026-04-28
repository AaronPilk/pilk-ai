"""Workflow engine — repeatable multi-step recipes.

A workflow is a YAML file on disk that names a sequence of steps.
Each step is one of:

  - ``tool``     — call a registered tool with inline args
  - ``agent``    — delegate to a master agent (operator-pulled)
  - ``approval`` — pause until the operator approves
  - ``note``     — write a markdown note into the brain

Workflows are LOADED from ``workflows/<name>/manifest.yaml`` at
boot, mirroring the agent registry pattern. Runs are tracked in
``workflow_runs`` + ``workflow_steps`` so the operator can
pause/resume/cancel.

This is the foundation. Three default workflows ship in v0:
  - ``daily_intelligence_brief``
  - ``ingest_file_to_brain``
  - ``research_topic_to_brain``

Marketing-specific workflows are explicitly NOT included in this
batch — those land later when the use case is concrete.
"""

from core.workflows.engine import (
    WorkflowEngine,
    WorkflowExecutionError,
)
from core.workflows.manifest import (
    Workflow,
    WorkflowStep,
    load_all_workflows,
)
from core.workflows.store import (
    WorkflowRun,
    WorkflowRunStep,
    WorkflowStore,
)

__all__ = [
    "Workflow",
    "WorkflowEngine",
    "WorkflowExecutionError",
    "WorkflowRun",
    "WorkflowRunStep",
    "WorkflowStep",
    "WorkflowStore",
    "load_all_workflows",
]
