import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import MemoryRow from "../components/MemoryRow";
import {
  addMemory,
  clearMemory,
  deleteMemory,
  distillMemory,
  fetchMemory,
  pilk,
  type MemoryEntry,
  type MemoryKind,
  type MemoryProposal,
} from "../state/api";
import { humanizeMemoryKind, memorySectionLabel } from "../lib/humanize";

const KINDS: MemoryKind[] = [
  "preference",
  "standing_instruction",
  "fact",
  "pattern",
];

const EMPTY_COPY: Record<MemoryKind, string> = {
  preference:
    "No preferences yet. When you tell PILK how you like to work, they'll show up here.",
  standing_instruction:
    "No standing instructions. Rules you want PILK to always follow will live here.",
  fact:
    "No facts retained yet. Small things worth remembering (birthdays, regular contacts, account numbers) go here.",
  pattern:
    "No patterns yet. Recurring workflows PILK has noticed will surface here over time.",
};

export default function Memory() {
  const [entries, setEntries] = useState<MemoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [composerKind, setComposerKind] = useState<MemoryKind>("preference");
  const [composerTitle, setComposerTitle] = useState("");
  const [composerBody, setComposerBody] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [pendingConfirm, setPendingConfirm] = useState(false);
  const [composerOpen, setComposerOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await fetchMemory();
      setEntries(r.entries);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    return pilk.onMessage((m) => {
      if (
        m.type === "memory.created" ||
        m.type === "memory.deleted" ||
        m.type === "memory.cleared"
      ) {
        load();
      }
    });
  }, [load]);

  const grouped = useMemo(() => {
    const out: Record<MemoryKind, MemoryEntry[]> = {
      preference: [],
      standing_instruction: [],
      fact: [],
      pattern: [],
    };
    for (const e of entries) {
      if (KINDS.includes(e.kind)) out[e.kind].push(e);
    }
    return out;
  }, [entries]);

  const total = entries.length;

  const submit = async () => {
    if (!composerTitle.trim()) return;
    setSubmitting(true);
    setErr(null);
    try {
      await addMemory({
        kind: composerKind,
        title: composerTitle.trim(),
        body: composerBody.trim(),
      });
      setComposerTitle("");
      setComposerBody("");
      setComposerOpen(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const onDelete = async (id: string) => {
    try {
      await deleteMemory(id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const onClearAll = async () => {
    if (!pendingConfirm) {
      setPendingConfirm(true);
      return;
    }
    try {
      await clearMemory();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setPendingConfirm(false);
    }
  };

  return (
    <div className="memory-page">
      <div className="memory-page-head">
        <div className="memory-page-hero">
          <div className="memory-eyebrow">Memory</div>
          <h1 className="memory-title">What PILK remembers about you</h1>
          <p className="memory-sub">
            The structured memory PILK references. Add or remove entries
            any time. PILK will not reference anything you haven't put
            here.
          </p>
        </div>
        <div className="memory-head-actions">
          <div className="memory-total">
            {total} {total === 1 ? "entry" : "entries"}
          </div>
          {total > 0 && (
            <button
              type="button"
              className={`memory-clear${pendingConfirm ? " memory-clear--confirm" : ""}`}
              onClick={onClearAll}
              onBlur={() => setPendingConfirm(false)}
            >
              {pendingConfirm ? "Tap again to confirm" : "Clear everything"}
            </button>
          )}
        </div>
      </div>

      <div className="memory-learn-row">
        <Link
          to="/chat?start=interview"
          className="memory-interview-cta"
        >
          <span className="memory-interview-cta-icon" aria-hidden>
            💬
          </span>
          <div className="memory-interview-cta-body">
            <div className="memory-interview-cta-title">
              Let PILK get to know you
            </div>
            <div className="memory-interview-cta-sub">
              Kicks off a conversational interview — PILK asks one
              question at a time, branches on your answers, and saves
              what it learns here with your confirmation.
            </div>
          </div>
          <span className="memory-interview-cta-chev" aria-hidden>
            →
          </span>
        </Link>

        <DistillPanel onSaved={load} />
      </div>

      {composerOpen ? (
        <section className="memory-composer">
          <div className="memory-composer-row">
            <label className="memory-field">
              <span className="memory-field-label">Kind</span>
              <select
                value={composerKind}
                onChange={(e) => setComposerKind(e.target.value as MemoryKind)}
                disabled={submitting}
              >
                {KINDS.map((k) => (
                  <option key={k} value={k}>
                    {humanizeMemoryKind(k)}
                  </option>
                ))}
              </select>
            </label>
            <label className="memory-field memory-field--wide">
              <span className="memory-field-label">Title</span>
              <input
                type="text"
                value={composerTitle}
                onChange={(e) => setComposerTitle(e.target.value)}
                placeholder="A short, human-readable label"
                maxLength={200}
                disabled={submitting}
              />
            </label>
          </div>
          <label className="memory-field">
            <span className="memory-field-label">Detail (optional)</span>
            <textarea
              value={composerBody}
              onChange={(e) => setComposerBody(e.target.value)}
              rows={2}
              placeholder="Add any nuance that would help PILK use this well."
              disabled={submitting}
            />
          </label>
          <div className="memory-composer-actions">
            <button
              type="button"
              className="memory-distill-skip"
              onClick={() => {
                setComposerOpen(false);
                setComposerTitle("");
                setComposerBody("");
              }}
              disabled={submitting}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn--primary"
              onClick={submit}
              disabled={submitting || !composerTitle.trim()}
            >
              {submitting ? "Saving…" : "Remember this"}
            </button>
          </div>
        </section>
      ) : (
        <button
          type="button"
          className="memory-add-btn"
          onClick={() => setComposerOpen(true)}
        >
          <span className="memory-add-btn-icon" aria-hidden>
            +
          </span>
          Add a memory entry manually
        </button>
      )}

      {err && <div className="memory-error">{err}</div>}

      {KINDS.map((k) => {
        const rows = grouped[k];
        return (
          <div key={k} className="memory-category">
            <div className="approvals-section-head">
              <h2>{memorySectionLabel(k)}</h2>
              {rows.length > 0 && (
                <span className="approvals-count">{rows.length}</span>
              )}
            </div>
            {loading ? (
              <div className="agents-empty">Reading memory…</div>
            ) : rows.length === 0 ? (
              <div className="agents-empty">{EMPTY_COPY[k]}</div>
            ) : (
              <div className="memory-grid">
                {rows.map((e) => (
                  <MemoryRow key={e.id} entry={e} onDelete={onDelete} />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** "Analyze recent conversations" panel on /memory. Asks the backend
 * to run a Haiku call across the last 30 plans, surfaces the proposed
 * memory entries, and lets the operator save each one individually. */
function DistillPanel({ onSaved }: { onSaved: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [proposals, setProposals] = useState<MemoryProposal[] | null>(null);
  const [savedIdx, setSavedIdx] = useState<Set<number>>(new Set());
  const [dismissedIdx, setDismissedIdx] = useState<Set<number>>(new Set());

  const run = async () => {
    setBusy(true);
    setErr(null);
    setProposals(null);
    setSavedIdx(new Set());
    setDismissedIdx(new Set());
    try {
      const r = await distillMemory(30);
      setProposals(r.proposals);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const save = async (i: number) => {
    if (proposals === null) return;
    const p = proposals[i];
    try {
      await addMemory({ kind: p.kind, title: p.title, body: p.body });
      setSavedIdx((prev) => new Set(prev).add(i));
      onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const skip = (i: number) => {
    setDismissedIdx((prev) => new Set(prev).add(i));
  };

  return (
    <div className="memory-distill-cta">
      <button
        type="button"
        className="memory-distill-btn"
        onClick={() => void run()}
        disabled={busy}
      >
        <span className="memory-distill-btn-icon" aria-hidden>
          🧠
        </span>
        <div className="memory-distill-btn-body">
          <div className="memory-distill-btn-title">
            {busy ? "Analyzing…" : "Analyze recent conversations"}
          </div>
          <div className="memory-distill-btn-sub">
            Let PILK skim your last 30 sessions and propose durable
            things worth remembering. Nothing saves until you approve.
          </div>
        </div>
      </button>

      {err && <div className="settings-error">{err}</div>}

      {proposals !== null && proposals.length === 0 && (
        <div className="memory-distill-empty">
          Nothing durable to extract yet — keep chatting with PILK and
          try again later.
        </div>
      )}

      {proposals !== null && proposals.length > 0 && (
        <ul className="memory-distill-list">
          {proposals.map((p, i) => {
            if (dismissedIdx.has(i)) return null;
            const saved = savedIdx.has(i);
            return (
              <li
                key={`${p.kind}-${i}`}
                className={`memory-distill-item${saved ? " memory-distill-item--saved" : ""}`}
              >
                <div className="memory-distill-item-head">
                  <span className={`memory-distill-kind memory-distill-kind--${p.kind}`}>
                    {humanizeMemoryKind(p.kind)}
                  </span>
                  <span className="memory-distill-confidence">
                    {Math.round(p.confidence * 100)}% confident
                  </span>
                </div>
                <div className="memory-distill-title">{p.title}</div>
                {p.body && (
                  <div className="memory-distill-body">{p.body}</div>
                )}
                {p.rationale && (
                  <div className="memory-distill-rationale">
                    <em>Why:</em> {p.rationale}
                  </div>
                )}
                <div className="memory-distill-actions">
                  {saved ? (
                    <span className="memory-distill-saved">✓ Saved</span>
                  ) : (
                    <>
                      <button
                        type="button"
                        className="memory-distill-skip"
                        onClick={() => skip(i)}
                      >
                        Skip
                      </button>
                      <button
                        type="button"
                        className="memory-distill-save"
                        onClick={() => void save(i)}
                      >
                        Save to memory
                      </button>
                    </>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
