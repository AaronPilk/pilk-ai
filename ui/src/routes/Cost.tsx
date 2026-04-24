import { useEffect, useState } from "react";
import {
  fetchCostEntries,
  fetchCostSummary,
  fetchSubscriptionUsage,
  pilk,
  type CostEntry,
  type CostSummary,
  type SubscriptionUsage,
} from "../state/api";

export default function Cost() {
  const [summary, setSummary] = useState<CostSummary | null>(null);
  const [entries, setEntries] = useState<CostEntry[]>([]);
  const [usage, setUsage] = useState<SubscriptionUsage | null>(null);

  const refresh = () => {
    fetchCostSummary().then(setSummary).catch(() => {});
    fetchCostEntries(50).then((r) => setEntries(r.entries)).catch(() => {});
    fetchSubscriptionUsage().then(setUsage).catch(() => {});
  };

  useEffect(() => {
    refresh();
    return pilk.onMessage((m) => {
      if (m.type === "cost.updated") refresh();
    });
  }, []);

  return (
    <div className="cost">
      <div className="bg-orb bg-orb--1" aria-hidden />
      <div className="bg-orb bg-orb--2" aria-hidden />
      <header className="cost-head">
        <div className="cost-eyebrow">Cost</div>
        <h1 className="cost-title">What PILK is costing you</h1>
        <p className="cost-sub">
          Your Anthropic subscription is used first, then API calls bill
          through. Everything below is your real spend — no subscription
          routing illusion on other providers.
        </p>
      </header>

      {usage && <SubscriptionRing usage={usage} />}

      <div className="cost-summary">
        <SummaryCard label="Today" value={summary?.day_usd ?? 0} tone="accent" />
        <SummaryCard label="7-day" value={summary?.week_usd ?? 0} />
        <SummaryCard label="30-day" value={summary?.month_usd ?? 0} />
        <SummaryCard label="All time" value={summary?.total_usd ?? 0} tone="muted" />
      </div>

      <section className="cost-entries">
        <header className="cost-entries-head">
          <h2>Recent calls</h2>
          <span className="cost-entries-count">{entries.length}</span>
        </header>
        {entries.length === 0 ? (
          <div className="cost-empty">No billable activity yet.</div>
        ) : (
          <div className="cost-table-wrap">
            <table className="cost-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Model</th>
                  <th className="cost-col-num">In</th>
                  <th className="cost-col-num">Out</th>
                  <th className="cost-col-num">USD</th>
                  <th>Plan</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e) => (
                  <tr key={e.id}>
                    <td>{new Date(e.occurred_at).toLocaleTimeString()}</td>
                    <td className="cost-cell-model">{e.model ?? "—"}</td>
                    <td className="cost-col-num">{e.input_tokens ?? 0}</td>
                    <td className="cost-col-num">{e.output_tokens ?? 0}</td>
                    <td className="cost-col-num cost-cell-usd">
                      ${e.usd.toFixed(6)}
                    </td>
                    <td className="cost-cell-plan">{e.plan_id ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function SummaryCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: "accent" | "muted";
}) {
  return (
    <div className={`cost-card${tone ? ` cost-card--${tone}` : ""}`}>
      <div className="cost-card-label">{label}</div>
      <div className="cost-card-value">${value.toFixed(4)}</div>
    </div>
  );
}

function SubscriptionRing({ usage }: { usage: SubscriptionUsage }) {
  const pct = Math.max(0, Math.min(100, usage.pct));
  const circ = 2 * Math.PI * 46; // r=46, viewBox 120
  const dash = (pct / 100) * circ;
  const toneVar =
    usage.severity === "hot"
      ? "var(--danger)"
      : usage.severity === "warn"
        ? "var(--warn)"
        : "var(--accent)";
  return (
    <section className="cost-usage">
      <div className="cost-usage-ring" aria-hidden>
        <svg viewBox="0 0 120 120" width="120" height="120">
          <circle
            cx="60"
            cy="60"
            r="46"
            fill="none"
            stroke="var(--border)"
            strokeWidth="6"
          />
          <circle
            cx="60"
            cy="60"
            r="46"
            fill="none"
            stroke={toneVar}
            strokeWidth="6"
            strokeLinecap="round"
            strokeDasharray={`${dash} ${circ - dash}`}
            transform="rotate(-90 60 60)"
            style={{ transition: "stroke-dasharray 600ms var(--ease-out)" }}
          />
          <text
            x="60"
            y="58"
            textAnchor="middle"
            fill="var(--text)"
            fontSize="22"
            fontWeight="600"
          >
            {usage.count}
          </text>
          <text
            x="60"
            y="78"
            textAnchor="middle"
            fill="var(--text-dim)"
            fontSize="10"
            letterSpacing="2"
          >
            of {usage.estimated_cap}
          </text>
        </svg>
      </div>
      <div className="cost-usage-body">
        <div className="cost-usage-eyebrow">Claude Max · 5h window</div>
        <div className="cost-usage-title">
          {pct.toFixed(0)}% of your subscription budget
        </div>
        <div className="cost-usage-breakdown">
          <span>
            <b>{usage.pilk_count ?? 0}</b> from PILK
          </span>
          <span>·</span>
          <span>
            <b>{usage.claude_code_count ?? 0}</b> from Claude Code CLI
          </span>
        </div>
        <div className="cost-usage-hint">
          Ring refreshes as you use PILK. When it fills, PILK falls through
          to paid API billing for Anthropic calls.
        </div>
      </div>
    </section>
  );
}
