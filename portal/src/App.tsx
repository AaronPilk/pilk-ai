import { Navigate, Route, Routes } from "react-router-dom";
import SignIn from "./routes/SignIn";
import AuthCallback from "./routes/AuthCallback";
import Dashboard from "./routes/Dashboard";
import NotConfigured from "./routes/NotConfigured";
import { isConfigured } from "./lib/supabase";
import { useSession } from "./lib/session";

export default function App() {
  if (!isConfigured) {
    return <NotConfigured />;
  }
  const { session, ready } = useSession();

  if (!ready) {
    return (
      <div className="portal-loading">
        <div className="portal-loading-dot" />
      </div>
    );
  }

  return (
    <Routes>
      <Route path="/auth/callback" element={<AuthCallback />} />
      <Route
        path="/signin"
        element={session ? <Navigate to="/" replace /> : <SignIn />}
      />
      <Route
        path="/"
        element={
          session ? <Dashboard session={session} /> : <Navigate to="/signin" replace />
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
