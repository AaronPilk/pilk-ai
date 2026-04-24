import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import CalendarCard from "../components/CalendarCard";
import ConnectedServiceCard from "../components/ConnectedServiceCard";
import InboxCard from "../components/InboxCard";
import MessagesCard from "../components/MessagesCard";
import VoiceOrb from "../components/VoiceOrb";
import {
  fetchAgents,
  fetchApprovals,
  fetchCostEntries,
  fetchCostSummary,
  fetchIntegrationsStatus,
  fetchLogs,
  fetchMemory,
  fetchPlans,
  pilk,
  type AgentRow,
  type CostEntry,
  type CostSummary,
  type GoogleIntegrationStatus,
  type LogEntry,
  type MemoryEntry,
  type PlanSummary,
} from "../state/api";
import {
  greetingFor,
  humanizeAgentName,
  humanizeAgentState,
} from "../lib/humanize";

interface Snapshot {
  agents: AgentRow[];
  plans: PlanSummary[];
  cost: CostSummary | null;
  costEntries: CostEntry[];
  memory: MemoryEntry[];
  logs: LogEntry[];
  pendingApprovals: number;
  google: GoogleIntegrationStatus | null;
}

// Executive-tier prompts. Shown three at a time, rotated on mount so
// Home doesn't feel static between visits. Order intentionally mixes
// personal, work, research, and orchestration intents.
const SUGGESTION_POOL = [
  "Summarize my unread email from the last 24 hours",
  "Draft a polite nudge to the last person I emailed about the contract",
  "Plan my afternoon around the calls I have today",
  "Find the cheapest nonstop flight to SF next Tuesday",
  "Build me a sales outreach agent",
  "Scan my downloads folder and propose a tidy layout",
  "Research a competitor and summarize in five bullets",
  "Pull every invoice I received this month into one table",
];

function pickSuggestions(pool: string[], n: number): string[] {
  // Fisher-Yates slice — small n, rerolled on each mount.
  const picked: string[] = [];
  const used = new Set<number>();
  while (picked.length < n && used.size < pool.length) {
    const idx = Math.floor(Math.random() * pool.length);
    if (used.has(idx)) continue;
    used.add(idx);
    picked.push(pool[idx]);
  }
  return picked;
}

function pulseLineFor(snap: Snapshot): string {
  const running = snap.plans.filter((p) => p.status === "running").length;
  const approvals = snap.pendingApprovals;
  const agents = snap.agents.length;
  if (running > 0 && approvals > 0) {
    return `${running} ${plural(running, "plan")} running · ${approvals} ${plural(approvals, "approval")} need${approvals === 1 ? "s" : ""} your eyes.`;
  }
  if (running > 0) {
    return `${running} ${plural(running, "plan")} running on your behalf.`;
  }
  if (approvals > 0) {
    return `${approvals} ${plural(approvals, "approval")} waiting for your eyes.`;
  }
  if (agents > 0) {
    return `${agents} ${plural(agents, "agent")} ready. Ask when you're ready.`;
  }
  return "Standing by. Ask me anything.";
}

function plural(n: number, word: string): string {
  return n === 1 ? word : `${word}s`;
}

/** Split "Good morning, Aaron" → ["Good morning,", "Aaron"] so the
 * operator's name renders in gradient-text while the rest stays
 * flat. Falls back gracefully for greetings without a comma. */
function splitGreeting(g: string): [string, string | null] {
  const idx = g.lastIndexOf(",");
  if (idx < 0) return [g, null];
  return [g.slice(0, idx + 1), g.slice(idx + 1).trim() || null];
}

export default function Home() {
  const [snap, setSnap] = useState<Snapshot>({
    agents: [],
    plans: [],
    cost: null,
    costEntries: [],
    memory: [],
    logs: [],
    pendingApprovals: 0,
    google: null,
  });

  useEffect(() => {
    const load = async () => {
      const [
        agents,
        plans,
        cost,
        costEntries,
        memory,
        logs,
        approvals,
        integrations,
      ] = await Promise.all([
        fetchAgents().catch(() => ({ agents: [] })),
        fetchPlans().catch(() => ({ plans: [], running_plan_id: null })),
        fetchCostSummary().catch(() => null),
        fetchCostEntries(6).catch(() => ({ entries: [] })),
        fetchMemory().catch(() => ({ entries: [], kinds: [] })),
        fetchLogs({ limit: 6 }).catch(() => ({ entries: [], next_before: null })),
        fetchApprovals().catch(() => ({ pending: [], recent: [] })),
        fetchIntegrationsStatus().catch(() => null),
      ]);
      setSnap({
        agents: agents.agents,
        plans: plans.plans,
        cost,
        costEntries: costEntries.entries,
        memory: memory.entries,
        logs: logs.entries,
        pendingApprovals: approvals.pending.length,
        // Home shows your real inbox, not PILK's operational mailbox.
        google: integrations?.google?.user ?? null,
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
        m.type === "approval.resolved" ||
        m.type === "log.appended" ||
        m.type === "memory.updated"
      ) {
        load();
      }
    });
  }, []);

  const running = snap.plans.filter((p) => p.status === "running").length;
  const recent = snap.plans.slice(0, 4);
  const agentCount = snap.agents.length;
  const today = snap.cost?.day_usd ?? 0;
  const pulse = pulseLineFor(snap);
  const suggestions = useMemo(() => pickSuggestions(SUGGESTION_POOL, 3), []);

  const greeting = greetingFor();
  const [greetHead, greetTail] = splitGreeting(greeting);

  return (
    <div className="home">
      <div className="bg-orb bg-orb--1" aria-hidden />
      <div className="bg-orb bg-orb--2" aria-hidden />
      <section className="home-hero">
        <div className="home-hero-meta">
          <div className="home-hero-eyebrow">Your command center</div>
          <h1 className="home-hero-greeting">
            {greetHead}
            {greetTail && (
              <>
                {" "}
                <span className="text-gradient">{greetTail}</span>
              </>
            )}
            .
          </h1>
          <div className="home-pulse">{pulse}</div>
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
                  <span className="home-agent-state">
                    {humanizeAgentState(a.state)}
                  </span>
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

        <CostSummaryCard summary={snap.cost} entries={snap.costEntries} />
        <MemorySummaryCard entries={snap.memory} />
        <LogsSummaryCard entries={snap.logs} />

        {snap.google?.linked ? (
          <InboxCard email={snap.google.email} />
        ) : (
          <ConnectCard
            title="Email"
            body="Your real inbox will appear here once you link your working Gmail. PILK will read and help triage it without ever sending from your address without approval."
            cta="Connect your Gmail"
          />
        )}
        <CalendarCard />
        <ConnectedServiceCard
          provider="slack"
          title="Slack"
          notConnectedBody="Send messages to channels and DMs as you. Every post still runs through your approval."
          chatPrompt="Draft a Slack message for me — tell me first which channel to send it to and the text, then ask me to approve before posting."
          ctaLabel="Draft a Slack message"
          manageHint="Posts always require approval."
        />
        <ConnectedServiceCard
          provider="linkedin"
          title="LinkedIn"
          notConnectedBody="Publish posts on your LinkedIn profile as you. Every post still runs through your approval."
          chatPrompt="Draft a LinkedIn post for me — propose the text and visibility, then ask me to approve before publishing."
          ctaLabel="Draft a LinkedIn post"
          manageHint="Posts always require approval."
        />
        <ConnectedServiceCard
          provider="x"
          title="X"
          notConnectedBody="Post tweets from your X account. 280 characters max; every post still runs through your approval."
          chatPrompt="Draft a tweet for me — propose the text (280 chars max), then ask me to approve before posting."
          ctaLabel="Draft a tweet"
          manageHint="Posts always require approval."
        />
        <ConnectedServiceCard
          provider="meta"
          title="Facebook Page"
          notConnectedBody="Publish posts on a Facebook Page you manage. Personal-profile posting was removed by Meta in 2018, so this only works with Pages."
          chatPrompt="Draft a Facebook Page post for me — propose the text, then ask me to approve before publishing."
          ctaLabel="Draft a Page post"
          manageHint="Pages only. Personal FB walls aren't supported by Meta's API."
        />
        <ConnectedServiceCard
          provider="meta"
          title="Instagram Business"
          notConnectedBody="Publish on an Instagram Business or Creator account linked to a Facebook Page. Personal IG accounts aren't supported by Meta's API."
          chatPrompt="Draft an Instagram Business post for me — propose the caption and ask for an image URL, then ask me to approve before publishing."
          ctaLabel="Draft an Instagram post"
          manageHint="IG Business/Creator only. Requires a publicly-hosted image URL."
        />
        <MessagesCard />
      </section>

      <section className="home-suggestions">
        <div className="home-card-eyebrow">Try asking PILK</div>
        <div className="home-suggest-strip">
          {suggestions.map((s) => (
            <Link
              key={s}
              to={`/chat?prompt=${encodeURIComponent(s)}`}
              className="home-suggest"
            >
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

function CostSummaryCard({
  summary,
  entries,
}: {
  summary: CostSummary | null;
  entries: CostEntry[];
}) {
  const day = summary?.day_usd ?? 0;
  const month = summary?.month_usd ?? 0;
  const top = entries[0];
  return (
    <Link to="/cost" className="home-card home-card--drill">
      <div className="home-card-head">
        <div className="home-card-eyebrow">Cost</div>
        <span className="home-card-link">Breakdown →</span>
      </div>
      <div className="home-card-stats">
        <div className="home-stat">
          <div className="home-stat-label">Today</div>
          <div className="home-stat-value">${day.toFixed(2)}</div>
        </div>
        <div className="home-stat">
          <div className="home-stat-label">Month</div>
          <div className="home-stat-value">${month.toFixed(2)}</div>
        </div>
      </div>
      {top ? (
        <div className="home-card-foot">
          Last call · {top.model ?? "unknown"} · ${top.usd.toFixed(4)}
        </div>
      ) : (
        <div className="home-card-foot">No cost entries yet.</div>
      )}
    </Link>
  );
}

function MemorySummaryCard({ entries }: { entries: MemoryEntry[] }) {
  const recent = entries.slice(0, 4);
  const totals = entries.reduce<Record<string, number>>((acc, e) => {
    acc[e.kind] = (acc[e.kind] ?? 0) + 1;
    return acc;
  }, {});
  const kindCounts = Object.entries(totals).sort((a, b) => b[1] - a[1]);
  return (
    <Link to="/memory" className="home-card home-card--drill">
      <div className="home-card-head">
        <div className="home-card-eyebrow">Memory</div>
        <span className="home-card-link">Open →</span>
      </div>
      {entries.length === 0 ? (
        <div className="home-card-empty">
          PILK hasn't saved anything yet. Tell him your preferences in Chat
          and he'll file them here.
        </div>
      ) : (
        <>
          <div className="home-mem-kinds">
            {kindCounts.slice(0, 4).map(([k, n]) => (
              <span key={k} className="home-mem-kind">
                <span className="home-mem-kind-label">{k}</span>
                <span className="home-mem-kind-count">{n}</span>
              </span>
            ))}
          </div>
          <ul className="home-mem-list">
            {recent.map((m) => (
              <li key={m.id} className="home-mem-row">
                <span className="home-mem-title">{m.title}</span>
                <span className="home-mem-kind-chip">{m.kind}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </Link>
  );
}

function LogsSummaryCard({ entries }: { entries: LogEntry[] }) {
  const recent = entries.slice(0, 5);
  return (
    <Link to="/logs" className="home-card home-card--drill">
      <div className="home-card-head">
        <div className="home-card-eyebrow">Logs</div>
        <span className="home-card-link">Full stream →</span>
      </div>
      {recent.length === 0 ? (
        <div className="home-card-empty">Nothing logged yet.</div>
      ) : (
        <ul className="home-logs-list">
          {recent.map((e) => (
            <li key={`${e.kind}-${e.id}`} className="home-logs-row">
              <span className={`home-logs-kind home-logs-kind--${e.kind}`}>
                {e.kind}
              </span>
              <span className="home-logs-msg" title={e.title}>
                {e.title}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Link>
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
