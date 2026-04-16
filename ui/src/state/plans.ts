import { useEffect, useState } from "react";
import { pilk, type PlanDetail, type Step } from "./api";

export function useLivePlans() {
  const [plans, setPlans] = useState<Record<string, PlanDetail>>({});
  const [order, setOrder] = useState<string[]>([]);

  useEffect(() => {
    return pilk.onMessage((m) => {
      if (m.type === "plan.created") {
        const plan: PlanDetail = { ...m, steps: [] };
        setPlans((prev) => ({ ...prev, [plan.id]: plan }));
        setOrder((prev) => (prev.includes(plan.id) ? prev : [...prev, plan.id]));
      } else if (m.type === "plan.step_added") {
        const step: Step = m;
        setPlans((prev) => {
          const plan = prev[step.plan_id];
          if (!plan) return prev;
          return {
            ...prev,
            [step.plan_id]: { ...plan, steps: [...plan.steps, step] },
          };
        });
      } else if (m.type === "plan.step_updated") {
        const step: Step = m;
        setPlans((prev) => {
          const plan = prev[step.plan_id];
          if (!plan) return prev;
          const steps = plan.steps.map((s) => (s.id === step.id ? step : s));
          return { ...prev, [step.plan_id]: { ...plan, steps } };
        });
      } else if (m.type === "plan.completed") {
        setPlans((prev) => {
          const plan = prev[m.id];
          if (!plan) return prev;
          return {
            ...prev,
            [m.id]: {
              ...plan,
              status: m.status,
              actual_usd: m.actual_usd,
              updated_at: m.updated_at,
            },
          };
        });
      } else if (m.type === "cost.updated" && m.plan_id) {
        setPlans((prev) => {
          const plan = prev[m.plan_id];
          if (!plan) return prev;
          return {
            ...prev,
            [m.plan_id]: { ...plan, actual_usd: m.plan_actual_usd ?? plan.actual_usd },
          };
        });
      }
    });
  }, []);

  const ordered = order.map((id) => plans[id]).filter(Boolean);
  return { plans, ordered };
}
