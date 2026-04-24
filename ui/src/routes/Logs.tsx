import { useCallback, useEffect, useMemo, useState } from "react";
import LogRow from "../components/LogRow";
import {
  fetchLogs,
  pilk,
  type LogEntry,
  type LogKind,
} from "../state/api";

type FilterKey = "all" | LogKind;

const FILTER_CHIPS: { key: FilterKey; label: string }[] = [
  { key: "all", label: "All" },
  { key: "plan", label: "Plans" },
  { key: "approval", label: "Approvals" },
  { key: "trust", label: "Trust" },
];

const PAGE_SIZE = 50;

export default function Logs() {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterKey>("all");
  const [query, setQuery] = useState("");
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  const load = useCallback(
    async (kind: FilterKey) => {
      setLoading(true);
      setErr(null);
      try {
        const r = await fetchLogs({
          kind: kind === "all" ? undefined : kind,
          limit: PAGE_SIZE,
        });
        setEntries(r.entries);
        setNextBefore(r.next_before);
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    load(filter);
    return pilk.onMessage((m) => {
      if (
        m.type === "plan.created" ||
        m.type === "plan.completed" ||
        m.type === "approval.created" ||
        m.type === "approval.resolved" ||
        m.type === "trust.revoked"
      ) {
        load(filter);
      }
    });
  }, [filter, load]);

  const loadMore = async () => {
    if (!nextBefore) return;
    setLoadingMore(true);
    try {
      const r = await fetchLogs({
        kind: filter === "all" ? undefined : filter,
        limit: PAGE_SIZE,
        before: nextBefore,
      });
      setEntries((prev) => [...prev, ...r.entries]);
      setNextBefore(r.next_before);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingMore(false);
    }
  };

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return entries;
    return entries.filter((e) => e.title.toLowerCase().includes(q));
  }, [entries, query]);

  const counts = useMemo(() => {
    const c: Record<FilterKey, number> = {
      all: entries.length,
      plan: 0,
      approval: 0,
      trust: 0,
    };
    for (const e of entries) c[e.kind] += 1;
    return c;
  }, [entries]);

  return (
    <div className="logs">
      <div className="bg-orb bg-orb--1" aria-hidden />
      <div className="bg-orb bg-orb--2" aria-hidden />
      <header className="logs-head">
        <div>
          <div className="logs-eyebrow">Logs</div>
          <h1 className="logs-title">Everything PILK has done</h1>
          <p className="logs-sub">
            A plain-English timeline of plans, approvals, and trust rules —
            newest first. Tap a row for a little more context.
          </p>
        </div>
      </header>

      <div className="logs-controls">
        <div className="logs-chips">
          {FILTER_CHIPS.map((c) => (
            <button
              key={c.key}
              type="button"
              className={`logs-chip${filter === c.key ? " logs-chip--active" : ""}`}
              onClick={() => setFilter(c.key)}
            >
              <span>{c.label}</span>
              <span className="logs-chip-count">{counts[c.key]}</span>
            </button>
          ))}
        </div>
        <input
          type="text"
          className="logs-search"
          placeholder="Search this timeline"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {err && <div className="logs-error">{err}</div>}

      {loading && entries.length === 0 ? (
        <div className="logs-empty">Reading the timeline…</div>
      ) : visible.length === 0 ? (
        <div className="logs-empty">
          Nothing here yet — ask PILK to do something and it'll show up.
        </div>
      ) : (
        <div className="logs-list">
          {visible.map((e) => (
            <LogRow key={e.id} entry={e} />
          ))}
        </div>
      )}

      {nextBefore && (
        <div className="logs-footer">
          <button
            type="button"
            className="logs-more"
            onClick={loadMore}
            disabled={loadingMore}
          >
            {loadingMore ? "Loading…" : "Load earlier"}
          </button>
        </div>
      )}
    </div>
  );
}
