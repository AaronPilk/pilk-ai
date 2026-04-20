import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import MemoryRow from "../components/MemoryRow";
import {
  addMemory,
  clearMemory,
  deleteMemory,
  fetchMemory,
  pilk,
  type MemoryEntry,
  type MemoryKind,
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
    <div className="memory">
      <header className="memory-head">
        <div>
          <div className="memory-eyebrow">Memory</div>
          <h1 className="memory-title">What PILK is currently retaining</h1>
          <p className="memory-sub">
            This is the structured memory you've curated for PILK. Add or
            remove entries any time. PILK will not reference anything you
            haven't put here.
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
      </header>

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
            Kicks off a conversational interview — PILK asks one question
            at a time, branches on your answers, and saves what it learns
            here with your confirmation.
          </div>
        </div>
        <span className="memory-interview-cta-chev" aria-hidden>
          →
        </span>
      </Link>

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
            className="btn btn--primary"
            onClick={submit}
            disabled={submitting || !composerTitle.trim()}
          >
            {submitting ? "Saving…" : "Remember this"}
          </button>
        </div>
      </section>

      {err && <div className="memory-error">{err}</div>}

      <div className="memory-sections">
        {KINDS.map((k) => {
          const rows = grouped[k];
          return (
            <section key={k} className="memory-section">
              <div className="memory-section-head">
                <div className="memory-section-title">
                  {memorySectionLabel(k)}
                </div>
                <div className="memory-section-count">
                  {rows.length} {rows.length === 1 ? "entry" : "entries"}
                </div>
              </div>
              {loading ? (
                <div className="memory-section-empty">Reading memory…</div>
              ) : rows.length === 0 ? (
                <div className="memory-section-empty">{EMPTY_COPY[k]}</div>
              ) : (
                <div className="memory-section-list">
                  {rows.map((e) => (
                    <MemoryRow key={e.id} entry={e} onDelete={onDelete} />
                  ))}
                </div>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}
