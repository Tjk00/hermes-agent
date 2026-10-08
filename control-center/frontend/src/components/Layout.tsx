import { useState, type ReactNode } from "react";
import { Badge, Button, Sheet } from "./ui";

export type NavItem = { route: string; label: string; icon: string; group?: string };

export const NAV: NavItem[] = [
  { route: "dashboard", label: "Dashboard", icon: "◉" },
  { route: "chat", label: "Chat", icon: "💬" },
  { route: "models", label: "Models", icon: "◆" },
  { route: "providers", label: "Providers", icon: "🔌", group: "Configure" },
  { route: "keys", label: "API Keys", icon: "🔑", group: "Configure" },
  { route: "memory", label: "Memory", icon: "🧠", group: "Agent" },
  { route: "skills", label: "Skills", icon: "✨", group: "Agent" },
  { route: "tools", label: "Tools", icon: "🛠", group: "Agent" },
  { route: "schedules", label: "Schedules", icon: "⏱", group: "Automation" },
  { route: "tasks", label: "Tasks", icon: "▤", group: "Automation" },
  { route: "sessions", label: "Conversations", icon: "🗂", group: "History" },
  { route: "logs", label: "Logs", icon: "≡", group: "System" },
  { route: "diagnostics", label: "Diagnostics", icon: "🩺", group: "System" },
  { route: "deploy", label: "Deployment", icon: "☁", group: "System" },
  { route: "setup", label: "Setup wizard", icon: "🧭", group: "System" },
  { route: "settings", label: "Settings", icon: "⚙", group: "System" },
];

const PRIMARY = ["dashboard", "chat", "models", "providers"];

export function Layout({
  route,
  navigate,
  children,
  statusChip,
  user,
  onSignOut,
}: {
  route: string;
  navigate: (route: string) => void;
  children: ReactNode;
  statusChip?: ReactNode;
  user?: { username: string; role: string } | null;
  onSignOut: () => void;
}) {
  const [moreOpen, setMoreOpen] = useState(false);
  const groups = Array.from(new Set(NAV.filter((item) => item.group).map((item) => item.group!)));

  return (
    <div className="min-h-full pb-24 lg:pb-6 lg:pl-64">
      {/* Desktop sidebar */}
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-64 flex-col border-r border-[var(--color-border)] bg-[var(--color-surface)] p-4 lg:flex">
        <div className="mb-4">
          <div className="text-sm font-semibold tracking-wide">HERMES</div>
          <div className="text-xs text-[var(--color-muted)]">Control Center</div>
        </div>
        <nav className="flex-1 space-y-4 overflow-y-auto">
          {groups.map((group) => (
            <div key={group}>
              <div className="px-2 pb-1 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">{group}</div>
              {NAV.filter((item) => item.group === group).map((item) => (
                <button
                  key={item.route}
                  onClick={() => navigate(item.route)}
                  className={`flex w-full items-center gap-2 rounded-xl px-2 py-2 text-left text-sm ${
                    route === item.route ? "bg-white/10 font-medium" : "text-[var(--color-muted)] hover:bg-white/5"
                  }`}
                >
                  <span className="w-5 text-center">{item.icon}</span>
                  {item.label}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="space-y-2 border-t border-[var(--color-border)] pt-3">
          {statusChip}
          {user && (
            <div className="flex items-center justify-between text-xs text-[var(--color-muted)]">
              <span className="truncate">{user.username} · {user.role}</span>
              <Button variant="ghost" onClick={onSignOut} className="!px-1 !text-xs">
                Sign out
              </Button>
            </div>
          )}
        </div>
      </aside>

      {/* Mobile header */}
      <header className="safe-top sticky top-0 z-20 border-b border-[var(--color-border)] bg-[var(--color-bg)]/95 backdrop-blur lg:hidden">
        <div className="flex items-center justify-between gap-2 px-3 py-2">
          <div className="min-w-0">
            <div className="text-[13px] font-semibold tracking-wide">HERMES CONTROL CENTER</div>
            <div className="truncate text-[11px] text-[var(--color-muted)]">
              {NAV.find((item) => item.route === route)?.label ?? route}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {statusChip}
            <Button variant="ghost" onClick={onSignOut} className="!px-2 !text-xs">
              ⎋
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto w-full max-w-5xl space-y-3 p-3 lg:p-5">{children}</main>

      {/* Mobile bottom navigation */}
      <nav className="safe-bottom fixed inset-x-0 bottom-0 z-30 border-t border-[var(--color-border)] bg-[var(--color-surface)]/97 backdrop-blur lg:hidden">
        <div className="grid grid-cols-5">
          {PRIMARY.map((routeName) => {
            const item = NAV.find((nav) => nav.route === routeName)!;
            return (
              <button
                key={routeName}
                onClick={() => navigate(routeName)}
                className={`tap flex flex-col items-center justify-center gap-0.5 py-1.5 text-[11px] ${
                  route === routeName ? "text-[var(--color-accent)]" : "text-[var(--color-muted)]"
                }`}
              >
                <span className="text-lg leading-none">{item.icon}</span>
                {item.label}
              </button>
            );
          })}
          <button
            onClick={() => setMoreOpen(true)}
            className="tap flex flex-col items-center justify-center gap-0.5 py-1.5 text-[11px] text-[var(--color-muted)]"
          >
            <span className="text-lg leading-none">⋯</span>
            More
          </button>
        </div>
      </nav>

      <Sheet open={moreOpen} title="All sections" onClose={() => setMoreOpen(false)}>
        <div className="grid grid-cols-2 gap-2">
          {NAV.map((item) => (
            <Button
              key={item.route}
              variant={route === item.route ? "primary" : "default"}
              onClick={() => {
                navigate(item.route);
                setMoreOpen(false);
              }}
            >
              <span>{item.icon}</span>
              {item.label}
            </Button>
          ))}
        </div>
      </Sheet>
    </div>
  );
}

export function RuntimeChip({ state, healthy }: { state?: string; healthy?: boolean }) {
  const tone = healthy ? "ok" : state === "starting" ? "warn" : state === "error" ? "error" : "neutral";
  return (
    <Badge tone={tone as "ok" | "warn" | "error" | "neutral"}>
      {healthy ? "RUNTIME UP" : (state ?? "unknown").toUpperCase()}
    </Badge>
  );
}
