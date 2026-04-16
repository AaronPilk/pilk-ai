import { useState } from "react";
import type { PlanDetail, Step } from "../state/api";

const STATUS_COLOR: Record<string, string> = {
  running: "#f0c050",
  completed: "#65d19b",
  failed: "#ff6b6b",
  pending: "#6b7183",
  paused: "#a2a8b8",
  cancelled: "#6b7183",
  awaiting_approval: "#f0c050",
  done: "#65d19b",
  skipped: "#6b7183",
};

export default function PlanCard({ plan }: { plan: PlanDetail }) {
  const [open, setOpen] = useState(true);
  const dot = STATUS_COLOR[plan.status] ?? "#6b7183";
  const running = plan.status === "running";
  const stepCount = plan.steps.length;
  return (
    <div className="plan">
      <button
        className="plan-head"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span
          className="plan-dot"
          style={{ background: dot, color: dot }}
        />
        <span className="plan-status">{plan.status.replace(/_/g, " ")}</span>
        <span className="plan-count">
          {stepCount} step{stepCount === 1 ? "" : "s"}
        </span>
        <span className="plan-cost">${plan.actual_usd.toFixed(4)}</span>
        <span className="plan-chev" aria-hidden>
          {open ? "▾" : "▸"}
        </span>
      </button>
      {open && (
        <ol className="plan-steps">
          {plan.steps.map((s) => (
            <StepRow key={s.id} step={s} />
          ))}
          {running && stepCount === 0 && (
            <li className="step step--placeholder">planning…</li>
          )}
        </ol>
      )}
    </div>
  );
}

function StepRow({ step }: { step: Step }) {
  const [expanded, setExpanded] = useState(false);
  const statusDot = STATUS_COLOR[step.status] ?? "#6b7183";
  const icon = step.kind === "tool" ? "⚙" : step.kind === "llm" ? "✎" : "·";
  return (
    <li className={`step step--${step.status}`}>
      <button
        className="step-head"
        onClick={() => setExpanded((e) => !e)}
        aria-expanded={expanded}
      >
        <span className="step-icon">{icon}</span>
        <span
          className="step-dot"
          style={{ background: statusDot, color: statusDot }}
        />
        <span className="step-desc">{step.description}</span>
        {step.cost_usd > 0 && (
          <span className="step-cost">${step.cost_usd.toFixed(4)}</span>
        )}
      </button>
      {expanded && (
        <div className="step-detail">
          {step.risk_class && (
            <div className="step-meta">Risk · {step.risk_class}</div>
          )}
          {step.input !== undefined && step.input !== null && (
            <pre className="step-io">
              {JSON.stringify(step.input, null, 2)}
            </pre>
          )}
          {step.output !== undefined && step.output !== null && (
            <pre className="step-io">
              {JSON.stringify(step.output, null, 2)}
            </pre>
          )}
          {step.error && <div className="step-error">{step.error}</div>}
        </div>
      )}
    </li>
  );
}
