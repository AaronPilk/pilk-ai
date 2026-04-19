import { NavLink } from "react-router-dom";

type Item = { to: string; label: string };
type Group = { heading: string; items: Item[] };

const groups: Group[] = [
  {
    heading: "Command",
    items: [
      { to: "/", label: "Home" },
      { to: "/chat", label: "Chat" },
    ],
  },
  {
    heading: "Operations",
    items: [
      { to: "/tasks", label: "Tasks" },
      { to: "/agents", label: "Agents" },
      { to: "/sandboxes", label: "Sandboxes" },
      { to: "/approvals", label: "Approvals" },
      { to: "/sentinel", label: "Sentinel" },
    ],
  },
  {
    heading: "Admin",
    items: [
      { to: "/cost", label: "Cost" },
      { to: "/memory", label: "Memory" },
      { to: "/logs", label: "Logs" },
      { to: "/settings", label: "Settings" },
    ],
  },
];

export default function LeftNav() {
  return (
    <nav className="nav">
      <div className="nav-brand">
        <span className="nav-brand-dot" aria-hidden />
        <span className="nav-brand-word">PILK</span>
      </div>
      <div className="nav-groups">
        {groups.map((g) => (
          <div className="nav-group" key={g.heading}>
            <div className="nav-group-heading">{g.heading}</div>
            <ul className="nav-list">
              {g.items.map((it) => (
                <li key={it.to}>
                  <NavLink
                    to={it.to}
                    end={it.to === "/"}
                    className={({ isActive }) =>
                      isActive ? "nav-item nav-item--active" : "nav-item"
                    }
                  >
                    <span className="nav-item-label">{it.label}</span>
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </nav>
  );
}
