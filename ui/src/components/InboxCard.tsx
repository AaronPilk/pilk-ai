import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchInboxGlance, type InboxGlance } from "../state/api";
import { prettySenderName, relativeTime } from "../lib/humanize";

const TRIAGE_PROMPT =
  "Summarize my unread email from the last 24 hours. Group by sender, flag anything that needs a reply today, and skip newsletters and receipts.";

export default function InboxCard({ email }: { email: string | null }) {
  const [glance, setGlance] = useState<InboxGlance | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const g = await fetchInboxGlance();
      setGlance(g);
      setFetchError(null);
    } catch (e) {
      setFetchError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const unread = glance?.unread ?? 0;
  const preview = glance?.preview ?? [];
  const apiError = glance?.error ?? null;
  const err = fetchError ?? apiError;

  return (
    <div className="home-card">
      <div className="home-card-head">
        <div className="home-card-eyebrow">Inbox</div>
        <div className="home-inbox-head-right">
          {email && <span className="home-inbox-account">{email}</span>}
          <button
            type="button"
            className="home-inbox-refresh"
            onClick={load}
            disabled={loading}
            title="Refresh"
            aria-label="Refresh inbox"
          >
            {loading ? "…" : "↻"}
          </button>
        </div>
      </div>

      {err ? (
        <div className="home-card-empty">Couldn't read inbox: {err}</div>
      ) : loading && !glance ? (
        <div className="home-card-empty">Reading your inbox…</div>
      ) : unread === 0 ? (
        <div className="home-card-empty">
          Inbox zero for the last 24 hours. Nothing unread.
        </div>
      ) : (
        <>
          <div className="home-inbox-count">
            <span className="home-inbox-count-value">{unread}</span>
            <span className="home-inbox-count-label">
              unread in the last 24 hours
            </span>
          </div>
          <ul className="home-inbox-list">
            {preview.map((p, i) => (
              <li key={i} className="home-inbox-row">
                <span className="home-inbox-from">
                  {prettySenderName(p.from)}
                </span>
                <span className="home-inbox-subject" title={p.subject}>
                  {p.subject}
                </span>
                <span className="home-inbox-time">
                  {relativeTime(p.received_at)}
                </span>
              </li>
            ))}
          </ul>
          <Link
            to={`/chat?prompt=${encodeURIComponent(TRIAGE_PROMPT)}`}
            className="home-inbox-cta"
          >
            Ask PILK to triage →
          </Link>
        </>
      )}
    </div>
  );
}
