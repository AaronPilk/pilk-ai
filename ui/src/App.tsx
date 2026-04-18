import { Route, Routes } from "react-router-dom";
import LeftNav from "./components/LeftNav";
import TopBar from "./components/TopBar";
import Home from "./routes/Home";
import Chat from "./routes/Chat";
import Tasks from "./routes/Tasks";
import Agents from "./routes/Agents";
import Sandboxes from "./routes/Sandboxes";
import Approvals from "./routes/Approvals";
import Cost from "./routes/Cost";
import Memory from "./routes/Memory";
import Logs from "./routes/Logs";
import Settings from "./routes/Settings";

export default function App() {
  return (
    <div className="app">
      <LeftNav />
      <div className="main">
        <TopBar />
        <div className="content">
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/tasks" element={<Tasks />} />
            <Route path="/agents" element={<Agents />} />
            <Route path="/sandboxes" element={<Sandboxes />} />
            <Route path="/approvals" element={<Approvals />} />
            <Route path="/cost" element={<Cost />} />
            <Route path="/memory" element={<Memory />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/settings" element={<Settings />} />
          </Routes>
        </div>
      </div>
    </div>
  );
}
