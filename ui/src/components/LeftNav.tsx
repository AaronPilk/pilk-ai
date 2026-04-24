import { NavLink } from "react-router-dom";
import {
  Home,
  Brain,
  MessageSquare,
  ListTodo,
  Users,
  Boxes,
  ShieldCheck,
  Settings,
  type LucideIcon,
} from "lucide-react";

type Item = { to: string; label: string; icon: LucideIcon };
type Group = { heading: string; items: Item[] };

// Intentional cull — per product direction:
//
// * Sentinel lost its tab; it now renders inline above the agent grid
//   and the top-bar incident badge covers the "is there trouble?" glance.
// * Cost, Memory, and Logs moved into Home as clickable cards. Their
//   routes still exist for drill-in, but they don't need nav real estate
//   of their own — nobody's day starts with "let me open Logs."
const groups: Group[] = [
  {
    heading: "Command",
    items: [
      { to: "/", label: "Home", icon: Home },
      { to: "/brain", label: "Brain", icon: Brain },
      { to: "/chat", label: "Chat", icon: MessageSquare },
    ],
  },
  {
    heading: "Operations",
    items: [
      { to: "/tasks", label: "Tasks", icon: ListTodo },
      { to: "/agents", label: "Agents", icon: Users },
      { to: "/sandboxes", label: "Sandboxes", icon: Boxes },
      { to: "/approvals", label: "Approvals", icon: ShieldCheck },
    ],
  },
  {
    heading: "Admin",
    items: [{ to: "/settings", label: "Settings", icon: Settings }],
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
                    <it.icon size={16} className="nav-item-icon" aria-hidden />
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
