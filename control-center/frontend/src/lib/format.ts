export function bytes(value?: number | null): string {
  if (!value || value <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`;
}

export function duration(seconds?: number | null): string {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.max(0, Math.floor(seconds));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

export function relativeTime(iso?: string | null): string {
  if (!iso) return "—";
  const then = Date.parse(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
  if (Number.isNaN(then)) return iso;
  const seconds = (Date.now() - then) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function shortDate(iso?: string | null): string {
  if (!iso) return "—";
  const then = Date.parse(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
  if (Number.isNaN(then)) return iso;
  return new Date(then).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export type CostBilling = "free_local" | "free_tier" | "paid" | "unknown";

export function costTone(billing?: string): { classes: string; short: string } {
  switch (billing) {
    case "free_local":
      return { classes: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30", short: "FREE · LOCAL" };
    case "free_tier":
      return { classes: "bg-sky-500/15 text-sky-300 border-sky-500/30", short: "LIMITED FREE" };
    case "paid":
      return { classes: "bg-amber-500/15 text-amber-300 border-amber-500/30", short: "PAID" };
    default:
      return { classes: "bg-slate-500/15 text-slate-300 border-slate-500/30", short: "UNVERIFIED" };
  }
}

export function levelTone(level?: string): string {
  switch ((level ?? "").toUpperCase()) {
    case "ERROR":
    case "CRITICAL":
      return "text-red-300";
    case "WARNING":
    case "WARN":
      return "text-amber-300";
    case "SECURITY":
      return "text-fuchsia-300";
    default:
      return "text-slate-300";
  }
}

export function truncate(text: string, max = 140): string {
  if (!text) return "";
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}
