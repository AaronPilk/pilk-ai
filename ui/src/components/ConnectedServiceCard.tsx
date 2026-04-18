import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  fetchConnectedAccounts,
  pilk,
  type ConnectedAccount,
} from "../state/api";

/**
 * Shared Home tile for services where PILK doesn't have a rich read
 * glance (Slack / LinkedIn / X today). Shows whether the account is
 * connected, the identity it represents, and a one-click action that
 * opens Chat with a prefilled prompt. Reads the accounts list directly
 * so it stays in sync when the user links or removes accounts.
 */
export default function ConnectedServiceCard({
  provider,
  title,
  notConnectedBody,
  chatPrompt,
  ctaLabel,
  manageHint,
  readyWhen,
  notReadyBody,
}: {
  provider: string;
  title: string;
  notConnectedBody: string;
  chatPrompt: string;
  ctaLabel: string;
  manageHint?: string;
  /**
   * Optional predicate for services where "connected" isn't enough
   * (Meta, where the tile is "ready" only if the user manages a Page
   * or has a linked IG Business account). When false, the tile shows
   * `notReadyBody` alongside the identity line instead of the CTA.
   */
  readyWhen?: (account: ConnectedAccount) => boolean;
  notReadyBody?: string;
}) {
  const [account, setAccount] = useState<ConnectedAccount | null | undefined>(
    undefined,
  );

  useEffect(() => {
    const load = () => {
      fetchConnectedAccounts()
        .then((r) => {
          const found = r.accounts.find(
            (a) => a.provider === provider && a.role === "user",
          );
          setAccount(found ?? null);
        })
        .catch(() => setAccount(null));
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
  }, [provider]);

  return (
    <div className="home-card">
      <div className="home-card-head">
        <div className="home-card-eyebrow">{title}</div>
        {account && (
          <span className="home-service-status home-service-status--ok">
            Connected
          </span>
        )}
      </div>

      {account === undefined ? (
        <div className="home-card-empty">Checking connection…</div>
      ) : account === null ? (
        <>
          <div className="home-connect-body">{notConnectedBody}</div>
          <Link to="/settings" className="home-connect-cta-link">
            Connect in Settings →
          </Link>
        </>
      ) : (
        <>
          <div className="home-service-identity">
            {account.email ?? account.username ?? account.label}
          </div>
          {readyWhen && !readyWhen(account) ? (
            <>
              <div className="home-connect-body">
                {notReadyBody ?? "Additional setup is needed on the provider."}
              </div>
              <Link to="/settings" className="home-connect-cta-link">
                Manage access in Settings →
              </Link>
            </>
          ) : (
            <>
              {manageHint && (
                <div className="home-service-hint">{manageHint}</div>
              )}
              <Link
                to={`/chat?prompt=${encodeURIComponent(chatPrompt)}`}
                className="home-inbox-cta"
              >
                {ctaLabel} →
              </Link>
            </>
          )}
        </>
      )}
    </div>
  );
}
