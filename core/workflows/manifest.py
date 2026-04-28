"""Workflow manifest schema + on-disk loader.

A workflow manifest is a YAML file at
``workflows/<name>/manifest.yaml``:

    name: daily_intelligence_brief
    description: Pull yesterday's intel into the brain.
    trigger: operator           # operator | cron | event
    inputs:
      - name: project_slug
        required: false
    steps:
      - name: pull_digest
        kind: tool
        tool: intelligence_digest_read
        args:
          since: "${inputs.since}"
          min_score: 50
      - name: write_brief
        kind: note
        path: ingested/intel/${run_id}.md
        body_template: |
          # Daily intel — ${run_id}
          ${steps.pull_digest.text}

YAML is parsed once at boot and cached in memory. Re-load on
operator request via ``POST /workflows/reload``. No workflow
auto-fires; ``trigger`` is metadata only in this batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class WorkflowInput:
    name: str
    description: str = ""
    required: bool = False
    default: Any = None


@dataclass
class WorkflowStep:
    name: str
    kind: str  # tool|agent|approval|note
    description: str = ""
    tool: str | None = None
    agent: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    path: str | None = None
    body_template: str | None = None
    approval_message: str | None = None


@dataclass
class Workflow:
    name: str
    description: str
    trigger: str = "operator"  # operator|cron|event
    inputs: list[WorkflowInput] = field(default_factory=list)
    steps: list[WorkflowStep] = field(default_factory=list)
    success_criteria: str = ""
    failure_behavior: str = "stop"  # stop|continue|retry
    manifest_path: str = ""

    @classmethod
    def from_yaml(
        cls, path: Path, *, raw: dict[str, Any] | None = None,
    ) -> Workflow:
        if raw is None:
            with path.open(encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"workflow {path} must be a YAML mapping at root"
            )
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"workflow {path} missing 'name'")
        description = str(raw.get("description") or "").strip()
        trigger = str(raw.get("trigger") or "operator").strip()
        if trigger not in ("operator", "cron", "event"):
            raise ValueError(
                f"workflow {name}: trigger must be operator|cron|event"
            )
        inputs: list[WorkflowInput] = []
        for raw_input in raw.get("inputs") or []:
            if not isinstance(raw_input, dict) or "name" not in raw_input:
                raise ValueError(
                    f"workflow {name}: each input needs 'name'"
                )
            inputs.append(
                WorkflowInput(
                    name=str(raw_input["name"]),
                    description=str(raw_input.get("description") or ""),
                    required=bool(raw_input.get("required", False)),
                    default=raw_input.get("default"),
                )
            )
        raw_steps = raw.get("steps") or []
        if not raw_steps:
            raise ValueError(f"workflow {name}: must declare at least one step")
        steps: list[WorkflowStep] = []
        for s in raw_steps:
            if not isinstance(s, dict):
                raise ValueError(
                    f"workflow {name}: step must be a mapping"
                )
            if "name" not in s or "kind" not in s:
                raise ValueError(
                    f"workflow {name}: step needs 'name' + 'kind'"
                )
            kind = str(s["kind"]).strip()
            if kind not in ("tool", "agent", "approval", "note"):
                raise ValueError(
                    f"workflow {name}: step {s['name']} has invalid "
                    f"kind {kind!r}"
                )
            if kind == "tool" and not s.get("tool"):
                raise ValueError(
                    f"workflow {name}: tool-kind step needs 'tool'"
                )
            if kind == "agent" and not s.get("agent"):
                raise ValueError(
                    f"workflow {name}: agent-kind step needs 'agent'"
                )
            if kind == "note" and not (s.get("path") or s.get("body_template")):
                raise ValueError(
                    f"workflow {name}: note-kind step needs 'path' "
                    f"and 'body_template'"
                )
            steps.append(
                WorkflowStep(
                    name=str(s["name"]),
                    kind=kind,
                    description=str(s.get("description") or ""),
                    tool=s.get("tool"),
                    agent=s.get("agent"),
                    args=dict(s.get("args") or {}),
                    path=s.get("path"),
                    body_template=s.get("body_template"),
                    approval_message=s.get("approval_message"),
                )
            )
        return cls(
            name=name,
            description=description,
            trigger=trigger,
            inputs=inputs,
            steps=steps,
            success_criteria=str(raw.get("success_criteria") or ""),
            failure_behavior=str(
                raw.get("failure_behavior") or "stop"
            ),
            manifest_path=str(path),
        )


def load_all_workflows(root: Path) -> list[Workflow]:
    """Walk ``root`` (e.g. ``workflows/``) and load every
    ``<dir>/manifest.yaml`` it finds. Underscore-prefixed dirs are
    treated as templates / archived and skipped, mirroring the
    agent loader's convention."""
    out: list[Workflow] = []
    if not root.exists():
        return out
    for sub in sorted(root.iterdir()):
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        manifest_path = sub / "manifest.yaml"
        if not manifest_path.exists():
            continue
        try:
            wf = Workflow.from_yaml(manifest_path)
        except Exception as e:  # noqa: BLE001
            # Skip a single bad manifest instead of failing boot.
            from core.logging import get_logger
            get_logger("pilkd.workflows").warning(
                "workflow_manifest_invalid",
                path=str(manifest_path),
                error=str(e),
            )
            continue
        out.append(wf)
    return out


__all__ = [
    "Workflow",
    "WorkflowInput",
    "WorkflowStep",
    "load_all_workflows",
]
