import { Navigate, Route, Routes } from "react-router-dom";
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
  return (
    <AuthGate>
      <div className="app">
        <LeftNav />
        <div className="main">
          <TopBar />
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
