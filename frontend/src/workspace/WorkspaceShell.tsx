import { NavLink, Outlet } from "react-router-dom";
import "./Workspace.css";

const NAV = [
  { to: "/", label: "Overview", end: true, icon: "home" },
  { to: "/inbox", label: "Inbox", end: false, icon: "inbox" },
  { to: "/briefings", label: "Briefings", end: false, icon: "calendar" },
  { to: "/assistant", label: "AI Assistant", end: false, icon: "chat" },
] as const;

function NavIcon({ name }: { name: (typeof NAV)[number]["icon"] }) {
  const common = {
    className: "ea-nav-icon",
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.75,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true as const,
  };

  switch (name) {
    case "home":
      return (
        <svg {...common}>
          <path d="M3 10.5 12 3l9 7.5" />
          <path d="M5 10v10h14V10" />
        </svg>
      );
    case "inbox":
      return (
        <svg {...common}>
          <path d="M4 6h16v12H4z" />
          <path d="M4 13h4l2 3h4l2-3h4" />
        </svg>
      );
    case "calendar":
      return (
        <svg {...common}>
          <rect x="3" y="5" width="18" height="16" rx="2" />
          <path d="M3 10h18M8 3v4M16 3v4" />
        </svg>
      );
    case "chat":
      return (
        <svg {...common}>
          <path d="M5 5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H9l-4 3v-3H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z" />
        </svg>
      );
    default:
      return null;
  }
}

export default function WorkspaceShell() {
  return (
    <div className="ea-workspace">
      <aside className="ea-sidebar" aria-label="Workspace">
        <p className="ea-sidebar-label">Workspace</p>
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) =>
              `ea-nav-link${isActive ? " active" : ""}`
            }
          >
            <NavIcon name={item.icon} />
            <span>{item.label}</span>
          </NavLink>
        ))}
      </aside>

      <div className="ea-main">
        <header className="ea-main-header">
          <div className="ea-brand" aria-label="büddy">
            <img
              className="ea-brand-icon"
              src="/buddy-logo.jpg"
              alt=""
              width={36}
              height={36}
            />
            <span className="ea-logo">
              <span className="ea-logo-dark">b</span>
              <span className="ea-logo-accent">ü</span>
              <span className="ea-logo-dark">ddy</span>
            </span>
          </div>
        </header>
        <div className="ea-main-body">
          <Outlet />
        </div>
      </div>
    </div>
  );
}
