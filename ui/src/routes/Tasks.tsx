import { useEffect, useState } from "react";
import { fetchPlan, fetchPlans, pilk, type PlanDetail, type PlanSummary } from "../state/api";
import PlanCard from "../components/PlanCard";
import { humanize } from "../lib/humanize";

export default function Tasks() {
  const [plans, setPlans] = useState<PlanSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<PlanDetail | null>(null);

  useEffect(() => {
    fetchPlans()
      .then((r) => {
        setPlans(r.plans);
        if (r.plans.length > 0) setSelectedId(r.plans[0].id);
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
      } else if (m.type === "plan.completed") {
        setPlans((prev) =>
          prev.map((p) =>
            p.id === m.id
              ? { ...p, status: m.status, updated_at: m.updated_at, actual_usd: m.actual_usd }
              : p
          )
        );
      }
    });
  }, []);

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

  return (
    <div className="tasks">
      <div className="tasks-list">
        <div className="tasks-list-head">Recent plans</div>
        {plans.length === 0 && <div className="tasks-empty">No plans yet.</div>}
        {plans.map((p) => (
          <button
            key={p.id}
            className={`tasks-row ${selectedId === p.id ? "tasks-row--active" : ""}`}
            onClick={() => setSelectedId(p.id)}
          >
            <div className="tasks-row-goal">{p.goal}</div>
            <div className="tasks-row-meta">
              <span className={`tasks-row-status tasks-row-status--${p.status}`}>
                {humanize(p.status)}
              </span>
              <span className="tasks-row-cost">${p.actual_usd.toFixed(4)}</span>
            </div>
          </button>
        ))}
      </div>
      <div className="tasks-detail">
        {detail ? (
          <>
            <div className="tasks-detail-head">
              <div className="tasks-detail-goal">{detail.goal}</div>
              <div className="tasks-detail-meta">
                <span>{humanize(detail.status)}</span>
                <span>${detail.actual_usd.toFixed(4)}</span>
                <span>{detail.steps.length} steps</span>
              </div>
            </div>
            <PlanCard plan={detail} />
          </>
        ) : (
          <div className="tasks-empty">Select a plan.</div>
        )}
      </div>
    </div>
  );
}
