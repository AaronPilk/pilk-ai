import { useEffect, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "./supabase";

/** Subscribe to the current Supabase auth session.
 *
 * Returns `{ session: null, ready: true }` in local mode so the UI can
 * render without waiting. In cloud mode, `ready` stays false until the
 * first `getSession()` promise resolves — routes that require auth
 * should gate on `ready` to avoid flashing the sign-in redirect.
 */
export function useSession(): { session: Session | null; ready: boolean } {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!supabase) {
      setReady(true);
      return;
    }
    let cancelled = false;
    supabase.auth.getSession().then(({ data }) => {
      if (cancelled) return;
      setSession(data.session);
      setReady(true);
    });
    const { data: sub } = supabase.auth.onAuthStateChange((_event, next) => {
      setSession(next);
    });
    return () => {
      cancelled = true;
      sub.subscription.unsubscribe();
    };
  }, []);

  return { session, ready };
}

/** Return a fresh Supabase access token for outbound API calls.
 *
 * `supabase-js` auto-refreshes when the cached token is close to expiry,
 * so calling `getSession()` here gives us the up-to-date JWT without us
 * having to manage timers ourselves. Returns null in local mode.
 */
export async function getAccessToken(): Promise<string | null> {
  if (!supabase) return null;
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}
