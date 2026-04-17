import { useState } from "react";
import type { PlanDetail, Step } from "../state/api";
import {
  humanizeAgentName,
  humanizeRiskClass,
  shortHost,
  shortenPath,
} from "../lib/humanize";

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
  const [showRaw, setShowRaw] = useState(false);
  const statusDot = STATUS_COLOR[step.status] ?? "#6b7183";
  const icon = step.kind === "tool" ? "⚙" : step.kind === "llm" ? "✎" : "·";
  const { title, summary } = humanizeStep(step);
  const outputText = extractOutputText(step);
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
        <span className="step-desc step-desc--human">{title}</span>
        {step.cost_usd > 0 && (
          <span className="step-cost">${step.cost_usd.toFixed(4)}</span>
        )}
      </button>
      {expanded && (
        <div className="step-detail">
          {step.risk_class && (
            <div className="step-meta">Risk · {humanizeRiskClass(step.risk_class)}</div>
          )}
          {summary && <div className="step-body">{summary}</div>}
          {outputText && <div className="step-body step-body--quote">{outputText}</div>}
          {step.error && <div className="step-error">{step.error}</div>}
          <button
            type="button"
            className="step-raw-toggle"
            onClick={() => setShowRaw((r) => !r)}
          >
            {showRaw ? "Hide raw data" : "Show raw data"}
          </button>
          {showRaw && (
            <>
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
            </>
          )}
        </div>
      )}
    </li>
  );
}

// ── humanizer ────────────────────────────────────────────────────────

function humanizeStep(step: Step): { title: string; summary?: string } {
  const desc = (step.description ?? "").trim();
  const input = (step.input ?? {}) as {
    url?: unknown;
    path?: unknown;
    command?: unknown;
    name?: unknown;
    amount?: unknown;
  };

  if (desc.startsWith("browser_session_open")) {
    return {
      title: "Opened a browser",
      summary: "PILK started a remote Chrome session. You can watch it on the Sandboxes tab.",
    };
  }
  if (desc.startsWith("browser_navigate")) {
    const url = typeof input.url === "string" ? input.url : null;
    return {
      title: url ? `Visited ${shortHost(url)}` : "Navigated in the browser",
      summary: url ? `Loaded ${url} and read the page.` : undefined,
    };
  }
  if (desc.startsWith("browser_session_close")) {
    return { title: "Closed the browser" };
  }
  if (desc.startsWith("fs_read")) {
    const path = typeof input.path === "string" ? input.path : null;
    return {
      title: path ? `Read ${shortenPath(path)}` : "Read a file",
      summary: path ? `Opened ${path}.` : undefined,
    };
  }
  if (desc.startsWith("fs_write")) {
    const path = typeof input.path === "string" ? input.path : null;
    return {
      title: path ? `Wrote ${shortenPath(path)}` : "Wrote a file",
      summary: path ? `Saved content to ${path}.` : undefined,
    };
  }
  if (desc.startsWith("shell_exec")) {
    const cmd = typeof input.command === "string" ? input.command : null;
    return {
      title: cmd ? `Ran shell: ${truncate(cmd.split(/\n/)[0], 70)}` : "Ran a shell command",
    };
  }
  if (desc.startsWith("net_fetch")) {
    const url = typeof input.url === "string" ? input.url : null;
    return {
      title: url ? `Fetched ${shortHost(url)}` : "Fetched a URL",
      summary: url ? `GET ${url}` : undefined,
    };
  }
  if (desc.startsWith("llm_ask")) {
    return {
      title: "Asked a helper model",
      summary: "PILK consulted a smaller model for a quick answer.",
    };
  }
  if (desc.startsWith("agent_create")) {
    const name = typeof input.name === "string" ? input.name : null;
    return {
      title: name ? `Created agent: ${humanizeAgentName(name)}` : "Created a specialist agent",
    };
  }
  if (desc.startsWith("finance_")) {
    const amt = input.amount;
    return {
      title: amt != null ? `Financial step ($${amt})` : "Financial step",
    };
  }
  if (desc.startsWith("trade_execute")) {
    return { title: "Executed a trade" };
  }
  if (/^plan turn \d+/i.test(desc) || step.kind === "llm") {
    const reason =
      typeof step.output === "object" && step.output && "stop_reason" in step.output
        ? String((step.output as any).stop_reason)
        : null;
    if (reason === "tool_use") {
      return { title: "Thinking", summary: "PILK chose the next tool to use." };
    }
    if (reason === "end_turn") {
      return { title: "Finished thinking", summary: "PILK wrapped up this turn." };
    }
    return { title: "Thinking" };
  }
  // Fallback — clean up common dev-y shapes.
  return { title: cleanDevDesc(desc) };
}

function extractOutputText(step: Step): string | null {
  const o = step.output;
  if (o == null) return null;
  if (typeof o === "string") return o.trim() || null;
  if (typeof o === "object") {
    const content = (o as any).content;
    if (typeof content === "string" && content.trim()) return content.trim();
  }
  return null;
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

function cleanDevDesc(s: string): string {
  // Strip `tool_name(args_blob)` → humanized tool name.
  const m = s.match(/^([a-z0-9_]+)\s*\(/i);
  if (m) return humanizeAgentName(m[1]);
  return s;
}
