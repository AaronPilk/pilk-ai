import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import VoiceOrb from "../components/VoiceOrb";
import {
  fetchAgents,
  fetchApprovals,
  fetchCostSummary,
  fetchPlans,
  pilk,
  type AgentRow,
  type CostSummary,
  type PlanSummary,
} from "../state/api";
import { greetingFor, humanizeAgentName } from "../lib/humanize";

interface Snapshot {
  agents: AgentRow[];
  plans: PlanSummary[];
  cost: CostSummary | null;
  pendingApprovals: number;
}

const SUGGESTIONS = [
  "Open a browser and visit example.com",
  "Build me a sales outreach agent",
  "Scan my downloads and propose a folder layout",
];

export default function Home() {
  const [snap, setSnap] = useState<Snapshot>({
    agents: [],
    plans: [],
    cost: null,
    pendingApprovals: 0,
  });

  useEffect(() => {
    const load = async () => {
      const [agents, plans, cost, approvals] = await Promise.all([
        fetchAgents().catch(() => ({ agents: [] })),
        fetchPlans().catch(() => ({ plans: [], running_plan_id: null })),
        fetchCostSummary().catch(() => null),
        fetchApprovals().catch(() => ({ pending: [], recent: [] })),
      ]);
      setSnap({
        agents: agents.agents,
        plans: plans.plans,
        cost,
        pendingApprovals: approvals.pending.length,
      });
    };
    load();
    return pilk.onMessage((m) => {
      if (
        m.type === "plan.created" ||
        m.type === "plan.completed" ||
        m.type === "agent.created" ||
        m.type === "cost.updated" ||
        m.type === "approval.created" ||
        m.type === "approval.resolved"
      ) {
        load();
      }
    });
  }, []);

  const running = snap.plans.filter((p) => p.status === "running").length;
  const recent = snap.plans.slice(0, 4);
  const agentCount = snap.agents.length;
  const today = snap.cost?.day_usd ?? 0;

  return (
    <div className="home">
      <section className="home-hero">
        <div className="home-hero-meta">
          <div className="home-hero-eyebrow">PILK · Command</div>
          <h1 className="home-hero-greeting">{greetingFor()}.</h1>
          <div className="home-hero-sub">
            Tap the orb or say "Hey PILK" when ambient listening is on.
          </div>
        </div>
        <VoiceOrb size="large" />
      </section>

      <section className="home-grid">
        <div className="home-card">
          <div className="home-card-eyebrow">Right now</div>
          <div className="home-card-stats">
            <Stat label="Running" value={String(running)} />
            <Stat
              label="Approvals"
              value={String(snap.pendingApprovals)}
              tone={snap.pendingApprovals > 0 ? "warn" : undefined}
              to="/approvals"
            />
            <Stat label="Today" value={`$${today.toFixed(2)}`} to="/cost" />
          </div>
        </div>

        <div className="home-card">
          <div className="home-card-head">
            <div className="home-card-eyebrow">Your workforce</div>
            <Link to="/agents" className="home-card-link">
              All agents →
            </Link>
          </div>
          {agentCount === 0 ? (
            <div className="home-card-empty">
              You don't have any specialist agents yet. Ask PILK in Chat — e.g.
              <em> "Build me a sales outreach agent."</em>
            </div>
          ) : (
            <ul className="home-agents">
              {snap.agents.slice(0, 5).map((a) => (
                <li key={a.name} className="home-agent">
                  <span
                    className="home-agent-orb"
                    data-state={a.state}
                    aria-hidden
                  />
                  <Link to="/agents" className="home-agent-name">
                    {humanizeAgentName(a.name)}
                  </Link>
                  <span className="home-agent-state">{a.state}</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="home-card">
          <div className="home-card-head">
            <div className="home-card-eyebrow">Recent activity</div>
            <Link to="/tasks" className="home-card-link">
              All tasks →
            </Link>
          </div>
          {recent.length === 0 ? (
            <div className="home-card-empty">No activity yet today.</div>
          ) : (
            <ul className="home-activity">
              {recent.map((p) => (
                <li key={p.id} className="home-activity-row">
                  <span
                    className={`home-activity-dot home-activity-dot--${p.status}`}
                  />
                  <Link to="/tasks" className="home-activity-goal" title={p.goal}>
                    {p.goal}
                  </Link>
                  <span className="home-activity-cost">
                    ${p.actual_usd.toFixed(2)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <ConnectCard
          title="Email"
          body="Unread summaries and drafts you need to send will appear here once Gmail is connected."
          cta="Connect Gmail"
        />
        <ConnectCard
          title="Calendar"
          body="Today's schedule, conflicts, and time you can give back will appear here once your calendar is connected."
          cta="Connect calendar"
        />
        <ConnectCard
          title="News & Intel"
          body="News and signals filtered for what actually matters to your work will appear here once sources are connected."
          cta="Connect sources"
        />
      </section>

      <section className="home-suggestions">
        <div className="home-card-eyebrow">Try asking PILK</div>
        <div className="home-suggest-strip">
          {SUGGESTIONS.map((s) => (
            <Link key={s} to="/chat" className="home-suggest">
              {s}
            </Link>
          ))}
        </div>
      </section>
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
  to,
}: {
  label: string;
  value: string;
  tone?: "warn" | "ok";
  to?: string;
}) {
  const body = (
    <>
      <div className="home-stat-label">{label}</div>
      <div className={`home-stat-value${tone ? ` home-stat-value--${tone}` : ""}`}>
        {value}
      </div>
    </>
  );
  return to ? (
    <Link to={to} className="home-stat home-stat--link">
      {body}
    </Link>
  ) : (
    <div className="home-stat">{body}</div>
  );
}

function ConnectCard({
  title,
  body,
  cta,
}: {
  title: string;
  body: string;
  cta: string;
}) {
  return (
    <div className="home-card home-card--connect">
      <div className="home-card-eyebrow">{title}</div>
      <div className="home-connect-body">{body}</div>
      <button type="button" className="home-connect-cta" disabled title="Coming soon">
        {cta}
      </button>
      <div className="home-connect-note">Not connected yet</div>
    </div>
  );
}
