import { NavLink } from "react-router-dom";

const items = [
  { to: "/chat", label: "Chat" },
  { to: "/tasks", label: "Tasks" },
  { to: "/agents", label: "Agents" },
  { to: "/sandboxes", label: "Sandboxes" },
  { to: "/approvals", label: "Approvals" },
  { to: "/cost", label: "Cost" },
  { to: "/memory", label: "Memory" },
  { to: "/logs", label: "Logs" },
  { to: "/settings", label: "Settings" },
];

export default function LeftNav() {
  return (
    <nav className="nav">
      <div className="nav-brand">PILK</div>
      <ul className="nav-list">
        {items.map((it) => (
          <li key={it.to}>
            <NavLink
              to={it.to}
              className={({ isActive }) =>
                isActive ? "nav-item nav-item--active" : "nav-item"
              }
            >
              {it.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
