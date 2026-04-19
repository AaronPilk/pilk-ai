import { useEffect, useState } from "react";
import {
  cancelPlan,
  fetchPlan,
  fetchPlans,
  pilk,
  type PlanDetail,
  type PlanStatus,
  type PlanSummary,
} from "../state/api";
import PlanCard from "../components/PlanCard";
import { humanize } from "../lib/humanize";

/** Relative-time formatter (no dayjs/date-fns dep). "Just now" under a
 * minute, "5 min ago" / "2 h ago" / "3 d ago" after that. The Tasks
 * gallery renders this so a new operator can tell at a glance whether
 * a plan just ran or has been sitting for a week. */
function timeAgo(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 45) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} d ago`;
  return new Date(iso).toLocaleDateString();
}

/** Emoji per plan status — mirrors the Agents gallery pattern so the
 * visual language stays consistent across the whole springboard. */
const STATUS_ICON: Record<PlanStatus, string> = {
  pending: "⋯",
  running: "▶",
  paused: "⏸",
  completed: "✓",
  failed: "✕",
  cancelled: "⊘",
};

/** Time-window session grouping (UI-only, no backend).
 *
 * Every chat turn creates its own Plan today, so a 30-message
 * conversation ends up as 30 "task" cards. Real fix is a Campaign
 * concept in PlanStore; until that lands we group plans that landed
 * within ``SESSION_GAP_MS`` of each other into a single "session" card.
 *
 * Plans arrive newest-first from the API, so we walk forward and open
 * a new bucket whenever the next plan is older than the last-seen one
 * by more than the gap.
 */
const SESSION_GAP_MS = 15 * 60 * 1000; // 15 minutes of idle = new session

interface TaskSession {
  plans: PlanSummary[];
  startedAt: string;
  endedAt: string;
  totalCostUsd: number;
  status: PlanStatus; // aggregated — see _aggregateStatus
  hasRunning: boolean;
}

function _aggregateStatus(plans: PlanSummary[]): PlanStatus {
  // Priority: running > failed > paused/pending > cancelled > completed
  const s = plans.map((p) => p.status);
  if (s.includes("running")) return "running";
  if (s.includes("failed")) return "failed";
  if (s.includes("paused")) return "paused";
  if (s.includes("pending")) return "pending";
  if (s.every((x) => x === "cancelled")) return "cancelled";
  return "completed";
}

function groupIntoSessions(plans: PlanSummary[]): TaskSession[] {
  const sessions: TaskSession[] = [];
  let bucket: PlanSummary[] = [];
  let lastTimeMs = Number.POSITIVE_INFINITY;

  const flush = () => {
    if (bucket.length === 0) return;
    const times = bucket.map((p) => new Date(p.created_at).getTime());
    sessions.push({
      plans: bucket,
      startedAt: new Date(Math.min(...times)).toISOString(),
      endedAt: new Date(Math.max(...times)).toISOString(),
      totalCostUsd: bucket.reduce((acc, p) => acc + (p.actual_usd ?? 0), 0),
      status: _aggregateStatus(bucket),
      hasRunning: bucket.some((p) => p.status === "running"),
    });
    bucket = [];
  };

  for (const p of plans) {
    const t = new Date(p.created_at).getTime();
    const gap = lastTimeMs - t;
    if (bucket.length > 0 && gap > SESSION_GAP_MS) {
      flush();
    }
    bucket.push(p);
    lastTimeMs = t;
  }
  flush();
  return sessions;
}

export default function Tasks() {
  const [plans, setPlans] = useState<PlanSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<PlanDetail | null>(null);
  const [runningPlanId, setRunningPlanId] = useState<string | null>(null);
  const [stopping, setStopping] = useState<string | null>(null);
  // Null = gallery of sessions. Number = showing that session's
  // sub-plan list. If the session has only one plan we skip this
  // screen and route straight to the plan detail.
  const [selectedSessionIdx, setSelectedSessionIdx] = useState<number | null>(
    null,
  );

  useEffect(() => {
    fetchPlans()
      .then((r) => {
        setPlans(r.plans);
        setRunningPlanId(r.running_plan_id);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    return pilk.onMessage((m) => {
      if (m.type === "plan.created") {
        setPlans((prev) => [
          {
            id: m.id,
            goal: m.goal,
            status: m.status,
            created_at: m.created_at,
            updated_at: m.updated_at,
            actual_usd: m.actual_usd ?? 0,
            estimated_usd: m.estimated_usd ?? null,
          },
          ...prev,
        ]);
        setSelectedId(m.id);
        setRunningPlanId(m.id);
      } else if (m.type === "plan.completed") {
        setPlans((prev) =>
          prev.map((p) =>
            p.id === m.id
              ? { ...p, status: m.status, updated_at: m.updated_at, actual_usd: m.actual_usd }
              : p
          )
        );
        setRunningPlanId((cur) => (cur === m.id ? null : cur));
        setStopping((cur) => (cur === m.id ? null : cur));
      } else if (m.type === "plan.cancelling") {
        setStopping(m.plan_id);
      }
    });
  }, []);

  const handleStop = async (planId: string) => {
    setStopping(planId);
    try {
      await cancelPlan(planId);
    } catch {
      setStopping((cur) => (cur === planId ? null : cur));
    }
  };

  useEffect(() => {
    if (!selectedId) return;
    let cancelled = false;
    const load = () =>
      fetchPlan(selectedId).then((d) => {
        if (!cancelled) setDetail(d);
      }).catch(() => {});
    load();
    const off = pilk.onMessage((m) => {
      if (
        (m.type === "plan.step_added" || m.type === "plan.step_updated") &&
        (m.plan_id === selectedId)
      ) {
        load();
      } else if (m.type === "plan.completed" && m.id === selectedId) {
        load();
      }
    });
    return () => {
      cancelled = true;
      off();
    };
  }, [selectedId]);

  const sessions = groupIntoSessions(plans);

  // Plan-detail pane (user drilled all the way in to one plan).
  if (selectedId !== null && detail) {
    const isLive =
      detail.id === runningPlanId && detail.status === "running";
    return (
      <div className="agents-page">
        <button
          type="button"
          className="agents-back"
          onClick={() => {
            setSelectedId(null);
            setDetail(null);
            // Leave selectedSessionIdx as-is so we land back on the
            // session's sub-plan list, not the whole gallery.
          }}
        >
          ← Back
        </button>
        <div className="agent-detail">
          <div className="agent-detail-hero">
            <div
              className={`agent-detail-avatar task-card-icon--${detail.status}`}
              aria-hidden
            >
              {STATUS_ICON[detail.status] ?? "?"}
            </div>
            <div className="agent-detail-hero-body">
              <div className="agent-detail-name">{detail.goal}</div>
              <div className="tasks-detail-meta">
                <span
                  className={`agent-card-status agent-card-status--${detail.status}`}
                >
                  <span className="agent-card-status-dot" />
                  {humanize(detail.status)}
                </span>
                <span>${detail.actual_usd.toFixed(4)}</span>
                <span>{detail.steps.length} steps</span>
                <span>Started {timeAgo(detail.created_at)}</span>
                {isLive && (
                  <button
                    className="tasks-detail-stop"
                    onClick={() => void handleStop(detail.id)}
                    disabled={stopping === detail.id}
                    title="Stop this plan — closes any active browser sessions."
                  >
                    {stopping === detail.id ? "Stopping…" : "Stop"}
                  </button>
                )}
              </div>
            </div>
          </div>
          <PlanCard plan={detail} />
        </div>
      </div>
    );
  }

  // Session-detail pane: a multi-plan session. Renders every plan in
  // the session as a sub-card so the operator can drill into any step.
  if (selectedSessionIdx !== null && sessions[selectedSessionIdx]) {
    const session = sessions[selectedSessionIdx];
    return (
      <div className="agents-page">
        <button
          type="button"
          className="agents-back"
          onClick={() => setSelectedSessionIdx(null)}
        >
          ← All tasks
        </button>
        <div className="agent-detail">
          <div className="agent-detail-hero">
            <div
              className={`agent-detail-avatar task-card-icon--${session.status}`}
              aria-hidden
            >
              {STATUS_ICON[session.status] ?? "?"}
            </div>
            <div className="agent-detail-hero-body">
              <div className="agent-detail-name">
                Session · {session.plans.length}{" "}
                {session.plans.length === 1 ? "plan" : "plans"}
              </div>
              <div className="tasks-detail-meta">
                <span
                  className={`agent-card-status agent-card-status--${session.status}`}
                >
                  <span className="agent-card-status-dot" />
                  {humanize(session.status)}
                </span>
                <span>${session.totalCostUsd.toFixed(4)}</span>
                <span>Started {timeAgo(session.startedAt)}</span>
              </div>
            </div>
          </div>
          <div className="agents-gallery tasks-gallery">
            {session.plans.map((p) => (
              <button
                key={p.id}
                className={`agent-card task-card task-card--${p.status}`}
                onClick={() => setSelectedId(p.id)}
              >
                <div className="task-card-head">
                  <span
                    className={`task-card-icon task-card-icon--${p.status}`}
                    aria-hidden
                  >
                    {STATUS_ICON[p.status] ?? "?"}
                  </span>
                  <span className="task-card-time">
                    {timeAgo(p.created_at)}
                  </span>
                </div>
                <div className="task-card-goal">{p.goal}</div>
                <div className="task-card-footer">
                  <span
                    className={`agent-card-status agent-card-status--${p.status}`}
                  >
                    <span className="agent-card-status-dot" />
                    {humanize(p.status)}
                  </span>
                  <span className="task-card-cost">
                    ${p.actual_usd.toFixed(4)}
                  </span>
                </div>
              </button>
            ))}
          </div>
        </div>
      </div>
    );
  }

  // Gallery — one card per *session*, not per plan.
  return (
    <div className="agents-page">
      <div className="agents-page-head">
        <h1>Tasks</h1>
        <p>
          One card per working session. Tap to see the individual steps
          and outputs.
        </p>
      </div>
      {plans.length === 0 ? (
        <div className="agents-empty">
          No plans yet. Ask PILK in Chat to do something.
        </div>
      ) : (
        <div className="agents-gallery tasks-gallery">
          {sessions.map((session, idx) => {
            const lead = session.plans[0];
            const multi = session.plans.length > 1;
            const open = () => {
              if (multi) {
                setSelectedSessionIdx(idx);
              } else {
                setSelectedId(lead.id);
              }
            };
            return (
              <button
                key={lead.id}
                className={`agent-card task-card task-card--${session.status}`}
                onClick={open}
              >
                <div className="task-card-head">
                  <span
                    className={`task-card-icon task-card-icon--${session.status}`}
                    aria-hidden
                  >
                    {STATUS_ICON[session.status] ?? "?"}
                  </span>
                  <span className="task-card-time">
                    {timeAgo(session.endedAt)}
                  </span>
                </div>
                <div className="task-card-goal">
                  {lead.goal}
                  {multi && (
                    <span className="task-card-more">
                      {" "}
                      + {session.plans.length - 1} more
                    </span>
                  )}
                </div>
                <div className="task-card-footer">
                  <span
                    className={`agent-card-status agent-card-status--${session.status}`}
                  >
                    <span className="agent-card-status-dot" />
                    {multi
                      ? `${session.plans.length} plans`
                      : humanize(session.status)}
                  </span>
                  <span className="task-card-cost">
                    ${session.totalCostUsd.toFixed(4)}
                  </span>
                </div>
                {session.hasRunning && (
                  <span className="task-card-session-live">Live</span>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
