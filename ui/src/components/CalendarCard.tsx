import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchCalendarGlance, type CalendarGlance } from "../state/api";
import { shortClock } from "../lib/humanize";

export default function CalendarCard() {
  const [glance, setGlance] = useState<CalendarGlance | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const g = await fetchCalendarGlance("user");
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

  const events = glance?.preview ?? [];
  const apiError = glance?.error ?? null;
  const err = fetchError ?? apiError;

  return (
    <div className="home-card">
      <div className="home-card-head">
        <div className="home-card-eyebrow">Calendar</div>
        {glance?.linked && !glance.scope_missing && (
          <button
            type="button"
            className="home-inbox-refresh"
            onClick={load}
            disabled={loading}
            title="Refresh"
            aria-label="Refresh calendar"
          >
            {loading ? "…" : "↻"}
          </button>
        )}
      </div>

      {err ? (
        <div className="home-card-empty">Couldn't read calendar: {err}</div>
      ) : loading && !glance ? (
        <div className="home-card-empty">Reading your calendar…</div>
      ) : !glance?.linked ? (
        <>
          <div className="home-connect-body">
            Today's schedule, conflicts, and time you can give back will
            appear here once your working Google account is connected.
          </div>
          <Link to="/settings" className="home-connect-cta-link">
            Connect in Settings →
          </Link>
        </>
      ) : glance.scope_missing ? (
        <>
          <div className="home-connect-body">
            Your Google account is connected, but Calendar access isn't
            enabled yet. Add it from Settings to see today's schedule
            here.
          </div>
          <Link to="/settings" className="home-connect-cta-link">
            Expand access · add Calendar →
          </Link>
        </>
      ) : events.length === 0 ? (
        <div className="home-card-empty">
          Nothing scheduled today. Enjoy the quiet.
        </div>
      ) : (
        <>
          <div className="home-inbox-count">
            <span className="home-inbox-count-value">
              {glance.events_count}
            </span>
            <span className="home-inbox-count-label">
              {glance.events_count === 1 ? "event today" : "events today"}
            </span>
          </div>
          <ul className="home-calendar-list">
            {events.map((e, i) => (
              <li key={i} className="home-calendar-row">
                <span className="home-calendar-time">
                  {shortClock(e.start)}
                </span>
                <span className="home-calendar-summary" title={e.summary}>
                  {e.summary}
                </span>
              </li>
            ))}
          </ul>
          <Link
            to="/chat?prompt=What%20do%20I%20have%20on%20my%20calendar%20today%3F"
            className="home-inbox-cta"
          >
            Ask PILK to plan around this →
          </Link>
        </>
      )}
    </div>
  );
}
