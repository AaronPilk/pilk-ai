import { useState } from "react";
import type { LogEntry } from "../state/api";
import {
  humanizeRiskClass,
  humanizeToolName,
  shortClock,
} from "../lib/humanize";

export default function LogRow({ entry }: { entry: LogEntry }) {
  const [open, setOpen] = useState(false);
  const sentence = renderSentence(entry);
  const meta = renderMeta(entry);
  return (
    <div className={`log-row log-row--${entry.kind}`}>
      <div className="log-row-time">{shortClock(entry.at)}</div>
      <div className="log-row-body">
        <div className="log-row-sentence">{sentence}</div>
        {meta && <div className="log-row-meta">{meta}</div>}
        {open && (
          <div className="log-row-details">{renderDetails(entry)}</div>
        )}
      </div>
      <div className="log-row-actions">
        <button
          type="button"
          className="log-row-toggle"
          onClick={() => setOpen((v) => !v)}
        >
          {open ? "Hide" : "Details"}
        </button>
      </div>
    </div>
  );
}

function kindLabel(kind: LogEntry["kind"]): string {
  switch (kind) {
    case "plan":
      return "Plan";
    case "approval":
      return "Approval";
    case "trust":
      return "Trust";
  }
}

function renderSentence(e: LogEntry): string {
  if (e.kind === "plan") {
    const status =
      e.status === "completed"
        ? "completed"
        : e.status === "failed"
          ? "failed"
          : e.status === "cancelled"
            ? "cancelled"
            : e.status === "running"
              ? "still running"
              : e.status;
    return `PILK ran a plan: "${e.title}" — ${status}.`;
  }
  if (e.kind === "approval") {
    const tool = humanizeToolName(e.title).toLowerCase();
    switch (e.status) {
      case "approved":
        return `You approved the action: ${tool}.`;
      case "rejected":
        return `You declined the action: ${tool}.`;
      case "pending":
        return `Waiting on your approval: ${tool}.`;
      case "expired":
        return `An approval expired: ${tool}.`;
    }
  }
  if (e.kind === "trust") {
    const tool = humanizeToolName(e.title).toLowerCase();
    return `PILK now auto-approves "${tool}" for ${humanizeDuration(e.ttl_seconds)}.`;
  }
  return "";
}

function renderMeta(e: LogEntry): string | null {
  const parts: string[] = [kindLabel(e.kind)];
  if (e.kind === "plan" && e.cost_usd > 0) {
    parts.push(`$${e.cost_usd.toFixed(2)}`);
  }
  if (e.kind === "approval") {
    parts.push(humanizeRiskClass(e.risk_class));
  }
  if (e.kind === "trust" && e.agent_name) {
    parts.push(`for ${e.agent_name}`);
  }
  return parts.join(" · ");
}

function renderDetails(e: LogEntry): string {
  if (e.kind === "approval" && e.reason) {
    return `Reason: ${e.reason}`;
  }
  if (e.kind === "trust") {
    const expiresAt = new Date(e.expires_at);
    const exp = Number.isNaN(expiresAt.getTime())
      ? e.expires_at
      : expiresAt.toLocaleString();
    return e.reason
      ? `${e.reason} · Expires ${exp}.`
      : `Expires ${exp}.`;
  }
  if (e.kind === "plan") {
    return `Plan id: ${e.plan_id}`;
  }
  return "No further details.";
}

function humanizeDuration(seconds: number): string {
  if (!seconds || seconds < 60) return `${seconds || 0}s`;
  const mins = Math.round(seconds / 60);
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"}`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"}`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"}`;
}
