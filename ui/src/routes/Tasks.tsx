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

export default function Tasks() {
  const [plans, setPlans] = useState<PlanSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<PlanDetail | null>(null);
  const [runningPlanId, setRunningPlanId] = useState<string | null>(null);
  const [stopping, setStopping] = useState<string | null>(null);

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

  // Gallery + detail pattern, matching /agents. Discovery-first: the
  // operator lands on a grid of plan cards and taps one to dive in.
  if (selectedId === null || !detail) {
    return (
      <div className="agents-page">
        <div className="agents-page-head">
          <h1>Tasks</h1>
          <p>
            Every plan PILK has run, most recent first. Tap a card to see
            its steps, cost, and outputs.
          </p>
        </div>
        {plans.length === 0 ? (
          <div className="agents-empty">
            No plans yet. Ask PILK in Chat to do something.
          </div>
        ) : (
          <div className="agents-gallery tasks-gallery">
            {plans.map((p) => {
              const isLive =
                p.id === runningPlanId && p.status === "running";
              return (
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
                  {isLive && (
                    <span
                      role="button"
                      className="task-card-stop"
                      onClick={(e) => {
                        e.stopPropagation();
                        void handleStop(p.id);
                      }}
                      aria-disabled={stopping === p.id}
                    >
                      {stopping === p.id ? "Stopping…" : "Stop"}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        )}
      </div>
    );
  }

  // Detail view
  const isLive = detail.id === runningPlanId && detail.status === "running";
  return (
    <div className="agents-page">
      <button
        type="button"
        className="agents-back"
        onClick={() => {
          setSelectedId(null);
          setDetail(null);
        }}
      >
        ← All tasks
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
