import { useEffect, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "./supabase";

export interface PortalUser {
  id: string;
  email: string | null;
  role: "master_admin" | "user" | "disabled" | null;
}

/** Subscribe to the current Supabase auth session. Returns null until the
 *  first hydration completes so callers can show a loading state. */
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

/** Read the app-level row from public.users for the signed-in caller.
 *  Returns null until the row is fetched (or if unauthenticated). */
export function usePortalUser(session: Session | null): PortalUser | null {
  const [user, setUser] = useState<PortalUser | null>(null);
  useEffect(() => {
    if (!supabase || !session) {
      setUser(null);
      return;
    }
    let cancelled = false;
    supabase
      .from("users")
      .select("id, email, role")
      .eq("id", session.user.id)
      .maybeSingle()
      .then(({ data, error }) => {
        if (cancelled) return;
        if (error) {
          setUser({
            id: session.user.id,
            email: session.user.email ?? null,
            role: null,
          });
          return;
        }
        if (!data) {
          // Row not yet created by the signup trigger (race on first
          // magic-link sign-in). Fall back to auth identity; the trigger
          // will catch up and a later reload resolves cleanly.
          setUser({
            id: session.user.id,
            email: session.user.email ?? null,
            role: null,
          });
          return;
        }
        setUser({
          id: data.id,
          email: data.email,
          role: data.role as PortalUser["role"],
        });
      });
    return () => {
      cancelled = true;
    };
  }, [session]);
  return user;
}
