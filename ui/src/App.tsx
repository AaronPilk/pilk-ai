import { useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import LeftNav from "./components/LeftNav";
import TopBar from "./components/TopBar";
import Home from "./routes/Home";
import Brain from "./routes/Brain";
import Chat from "./routes/Chat";
import Tasks from "./routes/Tasks";
import Agents from "./routes/Agents";
import Sandboxes from "./routes/Sandboxes";
import Approvals from "./routes/Approvals";
import Cost from "./routes/Cost";
import Memory from "./routes/Memory";
import Logs from "./routes/Logs";
import Settings from "./routes/Settings";
import { AuthGate } from "./lib/AuthGate";

export default function App() {
  // Mobile-only state — drives the slide-in nav drawer. On desktop
  // the CSS ignores ``data-nav-open`` and the sidebar is always
  // pinned, so flipping this on a wide screen is harmless.
  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();

  // Close the drawer whenever the route changes — picking a nav item
  // on mobile should land you on the page without the drawer staying
  // open over it.
  useEffect(() => {
    setNavOpen(false);
  }, [location.pathname]);

  return (
    <AuthGate>
      <div className="app" data-nav-open={navOpen ? "true" : "false"}>
        {/* Backdrop — visible only when the drawer is open on mobile;
            CSS hides it everywhere else. Click to close. */}
        <div
          className="nav-backdrop"
          aria-hidden={!navOpen}
          onClick={() => setNavOpen(false)}
        />
        <LeftNav />
        <div className="main">
          <TopBar
            navOpen={navOpen}
            onToggleNav={() => setNavOpen((v) => !v)}
          />
          <div className="content">
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/brain" element={<Brain />} />
              <Route path="/chat" element={<Chat />} />
              <Route path="/tasks" element={<Tasks />} />
              <Route path="/agents" element={<Agents />} />
              <Route path="/sandboxes" element={<Sandboxes />} />
              <Route path="/approvals" element={<Approvals />} />
              <Route path="/cost" element={<Cost />} />
              <Route path="/memory" element={<Memory />} />
              <Route path="/logs" element={<Logs />} />
              {/* Sentinel lost its dedicated tab — incidents live on the
                  Agents page (inline supervisor row) and the top-bar
                  badge. Old links redirect so anything bookmarked
                  still resolves. */}
              <Route path="/sentinel" element={<Navigate to="/agents" replace />} />
              <Route path="/settings" element={<Settings />} />
            </Routes>
          </div>
        </div>
      </div>
    </AuthGate>
  );
}
