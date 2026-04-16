import { useState } from "react";
import type { PlanDetail, Step } from "../state/api";

const STATUS_COLOR: Record<string, string> = {
  running: "#f1c40f",
  completed: "#4fbf7a",
  failed: "#e55a5a",
  pending: "#606775",
  paused: "#9ba2b0",
  cancelled: "#606775",
  awaiting_approval: "#e0b84a",
  done: "#4fbf7a",
  skipped: "#606775",
};

export default function PlanCard({ plan }: { plan: PlanDetail }) {
  const [open, setOpen] = useState(true);
  const dot = STATUS_COLOR[plan.status] ?? "#606775";
  const running = plan.status === "running";
  return (
    <div className="plan">
      <button className="plan-head" onClick={() => setOpen((o) => !o)}>
        <span className="plan-dot" style={{ background: dot }} />
        <span className="plan-status">{plan.status}</span>
        <span className="plan-count">
          {plan.steps.length} step{plan.steps.length === 1 ? "" : "s"}
        </span>
        <span className="plan-cost">${plan.actual_usd.toFixed(4)}</span>
        <span className="plan-chev">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <ol className="plan-steps">
          {plan.steps.map((s) => (
            <StepRow key={s.id} step={s} />
          ))}
          {running && plan.steps.length === 0 && (
            <li className="step step--placeholder">planning…</li>
          )}
        </ol>
      )}
    </div>
  );
}

function StepRow({ step }: { step: Step }) {
  const [expanded, setExpanded] = useState(false);
  const statusDot = STATUS_COLOR[step.status] ?? "#606775";
  const icon = step.kind === "tool" ? "⚙" : step.kind === "llm" ? "✎" : "·";
  return (
    <li className={`step step--${step.status}`}>
      <button className="step-head" onClick={() => setExpanded((e) => !e)}>
        <span className="step-icon">{icon}</span>
        <span className="step-dot" style={{ background: statusDot }} />
        <span className="step-desc">{step.description}</span>
        {step.cost_usd > 0 && (
          <span className="step-cost">${step.cost_usd.toFixed(4)}</span>
        )}
      </button>
      {expanded && (
        <div className="step-detail">
          {step.risk_class && (
            <div className="step-meta">risk: {step.risk_class}</div>
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
