import { useState } from "react";
import { supabase } from "../lib/supabase";

type Status =
  | { kind: "idle" }
  | { kind: "sending" }
  | { kind: "sent"; email: string }
  | { kind: "error"; message: string };

export default function SignIn() {
  const [email, setEmail] = useState("");
  const [status, setStatus] = useState<Status>({ kind: "idle" });

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!supabase) return;
    const trimmed = email.trim();
    if (!trimmed) return;
    setStatus({ kind: "sending" });
    const redirectTo = `${window.location.origin}/auth/callback`;
    const { error } = await supabase.auth.signInWithOtp({
      email: trimmed,
      options: { emailRedirectTo: redirectTo },
    });
    if (error) {
      setStatus({ kind: "error", message: error.message });
      return;
    }
    setStatus({ kind: "sent", email: trimmed });
  };

  return (
    <div className="portal-shell">
      <div className="portal-card">
        <h1 className="portal-logo">PILK</h1>
        <p className="portal-tagline">Local-first execution OS for agents.</p>
        {status.kind === "sent" ? (
          <div className="portal-block">
            <div className="portal-headline">Check your email</div>
            <p className="portal-body">
              We sent a magic link to <strong>{status.email}</strong>. Open it on
              this device to sign in.
            </p>
            <button
              className="portal-link"
              onClick={() => setStatus({ kind: "idle" })}
            >
              Use a different email
            </button>
          </div>
        ) : (
          <form className="portal-form" onSubmit={submit}>
            <label className="portal-label" htmlFor="email">
              Email
            </label>
            <input
              id="email"
              type="email"
              className="portal-input"
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={status.kind === "sending"}
              autoComplete="email"
              autoFocus
              required
            />
            <button
              type="submit"
              className="portal-btn"
              disabled={status.kind === "sending" || !email.trim()}
            >
              {status.kind === "sending" ? "Sending…" : "Send magic link"}
            </button>
            {status.kind === "error" && (
              <div className="portal-error">{status.message}</div>
            )}
            <p className="portal-fineprint">
              New here? The same link creates your account on first use.
            </p>
          </form>
        )}
      </div>
    </div>
  );
}
