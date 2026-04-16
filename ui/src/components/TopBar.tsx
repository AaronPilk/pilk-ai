import { useConnection } from "../state/api";

export default function TopBar() {
  const { status } = useConnection();
  const dot =
    status === "open" ? "#2ecc71" : status === "connecting" ? "#f1c40f" : "#e74c3c";
  return (
    <header className="topbar">
      <div className="topbar-left">
        <span className="topbar-conn">
          <span className="dot" style={{ background: dot }} />
          pilkd · {status}
        </span>
      </div>
      <div className="topbar-right">
        <span className="topbar-stat">running 0</span>
        <span className="topbar-stat">pending 0</span>
        <span className="topbar-stat">today $0.00</span>
      </div>
    </header>
  );
}
