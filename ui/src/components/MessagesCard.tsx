import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchMessagesGlance, type MessagesGlance } from "../state/api";
import { relativeTime } from "../lib/humanize";

/**
 * Home tile for Apple Messages. Reads ~/Library/Messages/chat.db
 * (local only, macOS only, Full Disk Access required). If the DB
 * isn't readable, surfaces the reason and the path we checked so the
 * user knows which permission to grant.
 */
export default function MessagesCard() {
  const [glance, setGlance] = useState<MessagesGlance | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const g = await fetchMessagesGlance();
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

  const available = glance?.available === true;
  const threads = glance?.threads ?? [];
  const apiError = glance?.error ?? null;
  const err = fetchError ?? apiError;

  return (
    <div className="home-card">
      <div className="home-card-head">
        <div className="home-card-eyebrow">Messages</div>
        {available && (
          <button
            type="button"
            className="home-inbox-refresh"
            onClick={load}
            disabled={loading}
            title="Refresh"
            aria-label="Refresh messages"
          >
            {loading ? "…" : "↻"}
          </button>
        )}
      </div>

      {err ? (
        <div className="home-card-empty">Couldn't read Messages: {err}</div>
      ) : loading && !glance ? (
        <div className="home-card-empty">Reading your Messages…</div>
      ) : !available ? (
        <>
          <div className="home-connect-body">
            {glance?.reason ??
              "Apple Messages reading works only on macOS. PILK needs Full Disk Access granted to the Python process."}
          </div>
          {glance?.db_path && (
            <div className="home-service-hint">
              Looked at {glance.db_path}
            </div>
          )}
        </>
      ) : threads.length === 0 ? (
        <div className="home-card-empty">No recent threads.</div>
      ) : (
        <>
          <ul className="home-messages-list">
            {threads.map((t) => (
              <li key={t.chat_id} className="home-messages-row">
                <span
                  className="home-messages-from"
                  title={t.is_group ? "Group" : "Direct"}
                >
                  {t.title}
                </span>
                <span className="home-messages-snippet" title={t.last_snippet}>
                  {t.last_from_me ? "You: " : ""}
                  {t.last_snippet}
                </span>
                <span className="home-messages-time">
                  {relativeTime(t.last_at)}
                </span>
              </li>
            ))}
          </ul>
          <Link
            to="/chat?prompt=Summarize%20my%20most%20recent%20iMessage%20threads%20and%20flag%20anything%20that%20needs%20a%20reply."
            className="home-inbox-cta"
          >
            Ask PILK to triage →
          </Link>
        </>
      )}
    </div>
  );
}
