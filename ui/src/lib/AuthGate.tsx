import { useEffect, type ReactNode } from "react";
import { isCloudMode, supabase } from "./supabase";
import { useSession } from "./session";

// Where the portal's sign-in page lives. Configurable via
// `VITE_PORTAL_URL` for preview deploys; defaults to production.
const PORTAL_URL =
  (import.meta.env.VITE_PORTAL_URL as string | undefined)?.replace(/\/$/, "") ??
  "https://pilk.ai";

/** Gate the dashboard behind a Supabase session in cloud mode.
 *
 * Local mode (`npm run dev` with no Supabase env) renders children
 * unconditionally so the pre-cloud developer loop still works.
 *
 * Cloud mode:
 *   - Wait for supabase-js to hydrate before deciding (avoids flashing
 *     the signed-out redirect on reload).
 *   - No session → bounce the browser to `pilk.ai/signin`. Auth lives
 *     on the portal; we don't duplicate the magic-link flow here.
 *   - Session present → render the app and expose a sign-out helper.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const { session, ready } = useSession();

  useEffect(() => {
    if (!isCloudMode || !ready || session) return;
    const next = encodeURIComponent(window.location.href);
    window.location.replace(`${PORTAL_URL}/signin?next=${next}`);
  }, [ready, session]);

  if (!isCloudMode) return <>{children}</>;

  if (!ready || !session) {
    return (
      <div className="auth-gate-loading">
        <div className="auth-gate-dot" />
      </div>
    );
  }

  return <>{children}</>;
}

/** Sign the caller out of Supabase and send them back to the portal. */
export async function signOutAndReturnToPortal() {
  if (supabase) await supabase.auth.signOut();
  window.location.replace(PORTAL_URL);
}
