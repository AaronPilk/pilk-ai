import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  fetchConnectedAccounts,
  pilk,
  type ConnectedAccount,
} from "../state/api";
import {
  humanizeCapability,
  humanizeIdentity,
  humanizeProvider,
} from "../lib/humanize";

function capabilitiesFor(account: ConnectedAccount): string[] {
  // Cheap scope-based derivation. We don't re-fetch the provider scope
  // group catalog; instead we read the scope URIs the account already
  // carries. Keeps this component fully client-side.
  const scopes = new Set(account.scopes);
  const out = new Set<string>();
  if (account.provider === "google") {
    if (Array.from(scopes).some((s) => s.includes("gmail."))) out.add("mail");
    if (Array.from(scopes).some((s) => s.includes("drive."))) out.add("drive");
    if (Array.from(scopes).some((s) => s.includes("calendar."))) {
      out.add("calendar");
    }
  }
  if (account.provider === "slack") out.add("messages");
  if (account.provider === "linkedin") out.add("posts");
  if (account.provider === "x") out.add("posts");
  return Array.from(out);
}

export default function ConnectedSummary() {
  const [accounts, setAccounts] = useState<ConnectedAccount[] | null>(null);

  useEffect(() => {
    const load = () => {
      fetchConnectedAccounts()
        .then((r) => setAccounts(r.accounts))
        .catch(() => setAccounts([]));
    };
    load();
    return pilk.onMessage((m) => {
      if (
        m.type === "account.linked" ||
        m.type === "account.removed" ||
        m.type === "account.default_changed"
      ) {
        load();
      }
    });
  }, []);

  return (
    <div className="home-card">
      <div className="home-card-head">
        <div className="home-card-eyebrow">Connected to</div>
        <Link to="/settings" className="home-card-link">
          Manage →
        </Link>
      </div>

      {accounts === null ? (
        <div className="home-card-empty">Checking connections…</div>
      ) : accounts.length === 0 ? (
        <>
          <div className="home-connect-body">
            PILK isn't connected to any of your working services yet.
            Connect your mail, calendar, or Slack to start.
          </div>
          <Link to="/settings" className="home-connect-cta-link">
            Connect an account →
          </Link>
        </>
      ) : (
        <div className="home-connected-groups">
          {(["system", "user"] as const).map((role) => {
            const rows = accounts.filter((a) => a.role === role);
            if (rows.length === 0) return null;
            return (
              <div key={role} className="home-connected-group">
                <div className="home-connected-group-label">
                  {humanizeIdentity(role)}
                </div>
                <div className="home-connected-chip-row">
                  {rows.flatMap((a) => {
                    const caps = capabilitiesFor(a);
                    if (caps.length === 0) {
                      return [
                        <span
                          key={a.account_id}
                          className="home-connected-chip"
                          title={a.email ?? a.label}
                        >
                          {humanizeProvider(a.provider)}
                        </span>,
                      ];
                    }
                    return caps.map((cap) => (
                      <span
                        key={`${a.account_id}:${cap}`}
                        className="home-connected-chip"
                        title={a.email ?? a.label}
                      >
                        {humanizeCapability(a.provider, cap)}
                      </span>
                    ));
                  })}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
