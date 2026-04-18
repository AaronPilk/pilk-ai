import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { supabase } from "../lib/supabase";

/** Supabase's auth client parses the fragment/hash from the magic-link URL
 *  on load (detectSessionInUrl=true). This route exists so we have a
 *  stable redirect_to target; once the session is detected we bounce to
 *  the dashboard. */
export default function AuthCallback() {
  const navigate = useNavigate();

  useEffect(() => {
    if (!supabase) {
      navigate("/signin", { replace: true });
      return;
    }
    const { data: sub } = supabase.auth.onAuthStateChange((event, session) => {
      if (event === "SIGNED_IN" && session) {
        navigate("/", { replace: true });
      } else if (event === "SIGNED_OUT") {
        navigate("/signin", { replace: true });
      }
    });
    // If the session is already in place by the time we mount, forward
    // immediately without waiting for an auth event.
    supabase.auth.getSession().then(({ data }) => {
      if (data.session) navigate("/", { replace: true });
    });
    return () => sub.subscription.unsubscribe();
  }, [navigate]);

  return (
    <div className="portal-loading">
      <div className="portal-loading-dot" />
      <div className="portal-loading-label">Signing you in…</div>
    </div>
  );
}
