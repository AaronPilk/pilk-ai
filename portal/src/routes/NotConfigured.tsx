/** Rendered when the portal bundle was built/served without
 *  VITE_SUPABASE_URL or VITE_SUPABASE_ANON_KEY. Kept as a visible page
 *  rather than a silent redirect so the operator sees exactly what's
 *  missing. */
export default function NotConfigured() {
  return (
    <div className="portal-shell">
      <div className="portal-card">
        <h1 className="portal-logo">PILK</h1>
        <div className="portal-block">
          <div className="portal-headline">Portal not configured</div>
          <p className="portal-body">
            This build is missing its Supabase environment variables. Set{" "}
            <code>VITE_SUPABASE_URL</code> and{" "}
            <code>VITE_SUPABASE_ANON_KEY</code>, then rebuild.
          </p>
          <p className="portal-body">
            See <code>docs/portal.md</code> for the full setup.
          </p>
        </div>
      </div>
    </div>
  );
}
