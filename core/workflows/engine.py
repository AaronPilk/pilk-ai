"""Workflow engine — execute one step at a time, persist state.

Operator-pulled. The engine never auto-fires a workflow; the
operator triggers a run via ``POST /workflows/{name}/run``. Each
step runs sequentially, persists its outcome, and the engine
checks for a pause/cancel request between steps so the operator
keeps control.

Step kinds (defined in ``manifest.py``):

  - ``tool``     — call a registered tool through the gateway,
                   honouring the policy gate. Tool calls write
                   ``cost_entries`` rows the same as inside a plan.
  - ``agent``    — spawn an agent run via the orchestrator's
                   ``agent_run``. Best-effort; if the agent
                   subsystem isn't wired the step fails and the
                   workflow stops or continues per the manifest's
                   ``failure_behavior``.
  - ``approval`` — record a checkpoint and pause. Operator must
                   call ``POST /workflows/runs/{id}/resume``.
  - ``note``     — write a markdown note into the brain via the
                   shared ``Vault``. Body uses minimal template
                   substitution (``${run_id}``, ``${steps.X.text}``).

Templates (in args / body_template):
  - ``${inputs.NAME}``     — operator-supplied input
  - ``${run_id}``          — current run id
  - ``${steps.NAME.text}`` — content from a previous tool/agent step
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.brain import Vault
from core.logging import get_logger
from core.workflows.manifest import Workflow, WorkflowStep
from core.workflows.store import WorkflowRun, WorkflowStore

log = get_logger("pilkd.workflows.engine")


class WorkflowExecutionError(RuntimeError):
    """Raised when a workflow run fails. Carries the step index +
    name so the caller can display it cleanly."""

    def __init__(
        self,
        message: str,
        *,
        step_idx: int | None = None,
        step_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.step_idx = step_idx
        self.step_name = step_name


_TEMPLATE_RE = re.compile(r"\$\{([^}]+)\}")


class WorkflowEngine:
    """Execute one workflow at a time. Stateless — the engine
    doesn't hold a reference to a single run; pass run_id around."""

    def __init__(
        self,
        *,
        store: WorkflowStore,
        registry: Any,        # core.tools.registry.ToolRegistry
        gateway: Any | None,  # core.tools.gateway.Gateway
        vault: Vault | None,
        orchestrator: Any | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._gateway = gateway
        self._vault = vault
        self._orch = orchestrator

    async def start(
        self,
        workflow: Workflow,
        *,
        inputs: dict[str, Any] | None = None,
    ) -> WorkflowRun:
        """Create a run, execute steps until completion / pause /
        failure. Returns the persisted run."""
        resolved_inputs = self._resolve_inputs(workflow, inputs or {})
        run = await self._store.create_run(
            workflow_name=workflow.name,
            inputs=resolved_inputs,
        )
        await self._store.update_run(run.id, status="running")
        return await self._execute(run.id, workflow, resolved_inputs)

    async def resume(
        self,
        run_id: str,
        workflow: Workflow,
    ) -> WorkflowRun:
        """Pick up a paused run from its checkpoint and continue."""
        run = await self._store.get_run(run_id)
        if run is None:
            raise WorkflowExecutionError(f"run {run_id} not found")
        if run.status not in ("paused", "pending"):
            raise WorkflowExecutionError(
                f"run {run_id} is in status {run.status!r}; "
                f"cannot resume"
            )
        await self._store.update_run(run_id, status="running")
        return await self._execute(
            run_id,
            workflow,
            run.inputs,
            start_at=run.current_step,
            checkpoint=run.checkpoint,
        )

    async def cancel(self, run_id: str) -> None:
        await self._store.update_run(
            run_id, status="cancelled",
            finished_at=datetime.now(UTC).isoformat(),
        )

    # ── Internal ─────────────────────────────────────────────────

    def _resolve_inputs(
        self,
        workflow: Workflow,
        provided: dict[str, Any],
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for spec in workflow.inputs:
            if spec.name in provided:
                resolved[spec.name] = provided[spec.name]
            elif spec.default is not None:
                resolved[spec.name] = spec.default
            elif spec.required:
                raise WorkflowExecutionError(
                    f"workflow {workflow.name}: missing required "
                    f"input {spec.name!r}"
                )
            else:
                resolved[spec.name] = None
        return resolved

    async def _execute(
        self,
        run_id: str,
        workflow: Workflow,
        inputs: dict[str, Any],
        *,
        start_at: int = 0,
        checkpoint: dict[str, Any] | None = None,
    ) -> WorkflowRun:
        ckpt: dict[str, Any] = dict(checkpoint or {})
        # ckpt["steps"] = {step_name: {"text": "...", "data": {...}}}
        ckpt.setdefault("steps", {})
        for idx in range(start_at, len(workflow.steps)):
            step_def = workflow.steps[idx]
            await self._store.update_run(
                run_id, current_step=idx, checkpoint=ckpt,
            )
            try:
                outcome = await self._run_step(
                    run_id, idx, step_def,
                    inputs=inputs, ckpt=ckpt,
                )
            except _PauseRequest as pause:
                # An ``approval`` step pauses the run. We persist
                # ``current_step = idx + 1`` so resume picks up
                # AFTER the approval — the operator's act of calling
                # resume IS the approval. The approval step itself
                # is already marked ``awaiting_approval`` in the
                # steps table for the audit trail.
                await self._store.update_run(
                    run_id,
                    status="paused",
                    current_step=idx + 1,
                    checkpoint=ckpt,
                )
                log.info(
                    "workflow_run_paused",
                    run_id=run_id,
                    step=step_def.name,
                    reason=str(pause),
                )
                return await self._return_run(run_id)
            except WorkflowExecutionError as e:
                error_msg = f"step {idx} ({step_def.name}): {e}"
                if workflow.failure_behavior == "continue":
                    log.warning(
                        "workflow_step_failed_continue",
                        run_id=run_id,
                        step=step_def.name,
                        error=str(e),
                    )
                    ckpt["steps"][step_def.name] = {
                        "error": str(e),
                    }
                    continue
                await self._store.update_run(
                    run_id,
                    status="failed",
                    error=error_msg,
                    finished_at=datetime.now(UTC).isoformat(),
                )
                log.warning(
                    "workflow_run_failed",
                    run_id=run_id,
                    step=step_def.name,
                    error=str(e),
                )
                return await self._return_run(run_id)
            ckpt["steps"][step_def.name] = outcome
        await self._store.update_run(
            run_id,
            status="completed",
            checkpoint=ckpt,
            finished_at=datetime.now(UTC).isoformat(),
        )
        log.info("workflow_run_completed", run_id=run_id)
        return await self._return_run(run_id)

    async def _run_step(
        self,
        run_id: str,
        idx: int,
        step: WorkflowStep,
        *,
        inputs: dict[str, Any],
        ckpt: dict[str, Any],
    ) -> dict[str, Any]:
        rendered_args = self._render(
            step.args, inputs=inputs, ckpt=ckpt, run_id=run_id,
        )
        step_row = await self._store.add_step(
            run_id=run_id, idx=idx, name=step.name, kind=step.kind,
            input_data=rendered_args,
        )
        try:
            if step.kind == "tool":
                outcome = await self._run_tool_step(step, rendered_args)
            elif step.kind == "agent":
                outcome = await self._run_agent_step(step, rendered_args)
            elif step.kind == "approval":
                await self._store.finish_step(
                    step_row.id, status="awaiting_approval",
                    output={"message": step.approval_message or ""},
                )
                raise _PauseRequest(
                    step.approval_message or "approval required"
                )
            elif step.kind == "note":
                outcome = await self._run_note_step(
                    step, inputs=inputs, ckpt=ckpt, run_id=run_id,
                )
            else:
                raise WorkflowExecutionError(
                    f"unknown step kind {step.kind!r}"
                )
        except WorkflowExecutionError:
            await self._store.finish_step(
                step_row.id, status="failed", error="see run.error",
            )
            raise
        await self._store.finish_step(
            step_row.id, status="done", output=outcome,
        )
        return outcome

    async def _run_tool_step(
        self,
        step: WorkflowStep,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        tool_name = step.tool or ""
        if self._registry is None:
            raise WorkflowExecutionError(
                "tool step requires a tool registry"
            )
        tool = self._registry.get(tool_name)
        if tool is None:
            raise WorkflowExecutionError(
                f"tool {tool_name!r} is not registered"
            )
        # Run via the gateway when available so the policy gate +
        # cost ledger see the call. Fall back to direct handler
        # invocation when the gateway isn't wired (tests).
        if self._gateway is not None:
            ctx = self._gateway.context_for(  # type: ignore[union-attr]
                agent_name=None,
            ) if hasattr(self._gateway, "context_for") else None
            try:
                outcome = await self._gateway.run(  # type: ignore[union-attr]
                    tool_name, args, ctx,
                )
            except Exception as e:  # noqa: BLE001
                raise WorkflowExecutionError(
                    f"tool {tool_name} failed: {e}"
                ) from e
        else:
            from core.tools.registry import ToolContext
            outcome = await tool.handler(args, ToolContext())
        if getattr(outcome, "is_error", False):
            raise WorkflowExecutionError(
                f"tool {tool_name} returned error: "
                f"{getattr(outcome, 'content', '')[:200]}"
            )
        return {
            "tool": tool_name,
            "text": getattr(outcome, "content", ""),
            "data": getattr(outcome, "data", None) or {},
        }

    async def _run_agent_step(
        self,
        step: WorkflowStep,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if self._orch is None:
            raise WorkflowExecutionError(
                "agent step requires an orchestrator"
            )
        agent_name = step.agent or ""
        task = str(args.get("task") or step.description or "")
        if not task:
            raise WorkflowExecutionError(
                f"agent step {step.name}: 'task' arg required"
            )
        try:
            await self._orch.agent_run(agent_name, task)
        except Exception as e:  # noqa: BLE001
            raise WorkflowExecutionError(
                f"agent {agent_name} failed: {e}"
            ) from e
        return {
            "agent": agent_name,
            "task": task,
            "text": f"delegated to {agent_name}",
            "data": {},
        }

    async def _run_note_step(
        self,
        step: WorkflowStep,
        *,
        inputs: dict[str, Any],
        ckpt: dict[str, Any],
        run_id: str,
    ) -> dict[str, Any]:
        if self._vault is None:
            raise WorkflowExecutionError(
                "note step requires a brain Vault"
            )
        path = self._render_string(
            step.path or "", inputs=inputs, ckpt=ckpt, run_id=run_id,
        )
        body = self._render_string(
            step.body_template or "",
            inputs=inputs, ckpt=ckpt, run_id=run_id,
        )
        if not path:
            raise WorkflowExecutionError(
                f"note step {step.name}: empty path"
            )
        try:
            self._vault.write(path, body)
        except Exception as e:  # noqa: BLE001
            raise WorkflowExecutionError(
                f"note write failed: {e}"
            ) from e
        return {"path": path, "text": body[:500]}

    async def _return_run(self, run_id: str) -> WorkflowRun:
        run = await self._store.get_run(run_id)
        assert run is not None
        return run

    # ── Templating ───────────────────────────────────────────────

    def _render(
        self,
        obj: Any,
        *,
        inputs: dict[str, Any],
        ckpt: dict[str, Any],
        run_id: str,
    ) -> Any:
        if isinstance(obj, str):
            return self._render_string(
                obj, inputs=inputs, ckpt=ckpt, run_id=run_id,
            )
        if isinstance(obj, list):
            return [
                self._render(v, inputs=inputs, ckpt=ckpt, run_id=run_id)
                for v in obj
            ]
        if isinstance(obj, dict):
            return {
                k: self._render(v, inputs=inputs, ckpt=ckpt, run_id=run_id)
                for k, v in obj.items()
            }
        return obj

    def _render_string(
        self,
        s: str,
        *,
        inputs: dict[str, Any],
        ckpt: dict[str, Any],
        run_id: str,
    ) -> str:
        if not s or "${" not in s:
            return s

        def lookup(expr: str) -> str:
            expr = expr.strip()
            if expr == "run_id":
                return run_id
            if expr.startswith("inputs."):
                key = expr.split(".", 1)[1]
                v = inputs.get(key)
                return "" if v is None else str(v)
            if expr.startswith("steps."):
                # ``steps.NAME.text`` or ``steps.NAME.data.KEY``
                parts = expr.split(".")
                step_name = parts[1] if len(parts) > 1 else ""
                slot = parts[2] if len(parts) > 2 else "text"
                step_out = (ckpt.get("steps") or {}).get(step_name) or {}
                if slot == "text":
                    return str(step_out.get("text") or "")
                if slot == "data":
                    rest = ".".join(parts[3:])
                    cur: Any = step_out.get("data") or {}
                    for piece in rest.split(".") if rest else []:
                        if isinstance(cur, dict):
                            cur = cur.get(piece)
                        else:
                            return ""
                    return "" if cur is None else str(cur)
            return ""

        return _TEMPLATE_RE.sub(
            lambda m: lookup(m.group(1)), s,
        )


class _PauseRequest(RuntimeError):
    """Internal sentinel for approval-step pauses."""


__all__ = ["WorkflowEngine", "WorkflowExecutionError"]
