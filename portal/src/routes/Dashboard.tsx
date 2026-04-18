import type { Session } from "@supabase/supabase-js";
import { supabase } from "../lib/supabase";
import { usePortalUser } from "../lib/session";

export default function Dashboard({ session }: { session: Session }) {
  const user = usePortalUser(session);
  const email = user?.email ?? session.user.email ?? "";
  const role = user?.role;

  const signOut = async () => {
    if (!supabase) return;
    await supabase.auth.signOut();
  };

  return (
    <div className="portal-shell">
      <div className="portal-card portal-card--wide">
        <div className="portal-header">
          <h1 className="portal-logo portal-logo--sm">PILK</h1>
          <button className="portal-link" onClick={signOut}>
            Sign out
          </button>
        </div>
        <div className="portal-block">
          <div className="portal-headline">Welcome{email ? `, ${email}` : ""}.</div>
          {role === "master_admin" && (
            <span className="portal-badge portal-badge--master">
              Master admin
            </span>
          )}
          <p className="portal-body">
            Your PILK account is ready. Open the dashboard to talk to
            your agents, kick off tasks, and review what they've done —
            from any browser, on any device.
          </p>
        </div>
        <div className="portal-block">
          <a className="portal-cta" href="https://app.pilk.ai">
            Open dashboard →
          </a>
          <p className="portal-body portal-body--muted">
            Hosted at{" "}
            <a
              className="portal-inline-link"
              href="https://app.pilk.ai"
            >
              app.pilk.ai
            </a>
            . Running pilkd locally instead? The dev dashboard is still at{" "}
            <a
              className="portal-inline-link"
              href="http://127.0.0.1:1420"
              target="_blank"
              rel="noreferrer"
            >
              127.0.0.1:1420
            </a>
            .
          </p>
        </div>
        <div className="portal-block portal-block--muted">
          <div className="portal-section-head">Coming next</div>
          <ul className="portal-list">
            <li>Bring-your-own API keys (Anthropic, Browserbase, Google).</li>
            <li>Per-account isolation when other people sign up.</li>
            <li>Billing + plan management.</li>
          </ul>
        </div>
      </div>
    </div>
  );
}
