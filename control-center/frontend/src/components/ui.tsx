import type { ReactNode } from "react";
import { costTone, type CostBilling } from "../lib/format";
import type { Toast } from "../lib/hooks";

export function Card({
  title,
  subtitle,
  actions,
  children,
  className = "",
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card p-3.5 ${className}`}>
      {(title || actions) && (
        <header className="mb-2.5 flex items-start justify-between gap-2">
          <div className="min-w-0">
            {title && <h2 className="truncate text-[15px] font-semibold tracking-wide">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-[var(--color-muted)]">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "ok" | "warn" | "error" | "info";
}) {
  const tones: Record<string, string> = {
    neutral: "bg-white/5 text-[var(--color-muted)] border-white/10",
    ok: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
    warn: "bg-amber-500/15 text-amber-300 border-amber-500/30",
    error: "bg-red-500/15 text-red-300 border-red-500/30",
    info: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  };
  return <span className={`chip ${tones[tone]}`}>{children}</span>;
}

export function CostChip({ billing, label }: { billing?: string; label?: string }) {
  const tone = costTone(billing as CostBilling);
  return <span className={`chip ${tone.classes}`}>{label ?? tone.short}</span>;
}

export function Button({
  children,
  onClick,
  variant = "default",
  disabled,
  type = "button",
  className = "",
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "default" | "primary" | "danger" | "ghost";
  disabled?: boolean;
  type?: "button" | "submit";
  className?: string;
  title?: string;
}) {
  const variants: Record<string, string> = {
    default: "bg-white/5 hover:bg-white/10 border-white/10 text-[var(--color-text)]",
    primary: "bg-[var(--color-accent)] text-black hover:brightness-105 border-transparent font-semibold",
    danger: "bg-red-500/15 text-red-200 hover:bg-red-500/25 border-red-500/30",
    ghost: "bg-transparent border-transparent text-[var(--color-muted)] hover:text-[var(--color-text)]",
  };
  return (
    <button
      type={type}
      title={title}
      onClick={onClick}
      disabled={disabled}
      className={`tap inline-flex items-center justify-center gap-1.5 rounded-xl border px-3 py-2 text-sm transition disabled:cursor-not-allowed disabled:opacity-40 ${variants[variant]} ${className}`}
    >
      {children}
    </button>
  );
}

export function Input({
  value,
  onChange,
  placeholder,
  type = "text",
  label,
  hint,
  autoFocus,
  disabled,
  inputMode,
  min,
  max,
}: {
  value: string | number;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: string;
  label?: string;
  hint?: string;
  autoFocus?: boolean;
  disabled?: boolean;
  inputMode?: "text" | "numeric" | "email" | "url";
  min?: number;
  max?: number;
}) {
  return (
    <label className="block">
      {label && <span className="mb-1 block text-xs font-medium text-[var(--color-muted)]">{label}</span>}
      <input
        className="tap w-full rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-2)] px-3 py-2.5 text-[15px] outline-none focus:border-[var(--color-accent-2)] disabled:opacity-50"
        value={value}
        type={type}
        inputMode={inputMode}
        min={min}
        max={max}
        autoFocus={autoFocus}
        disabled={disabled}
        placeholder={placeholder}
        autoComplete="off"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        onChange={(event) => onChange(event.target.value)}
      />
      {hint && <span className="mt-1 block text-xs text-[var(--color-muted)]">{hint}</span>}
    </label>
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  description,
  disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  description?: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="tap flex w-full items-center justify-between gap-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-2)] px-3 py-2.5 text-left disabled:opacity-50"
    >
      <span className="min-w-0">
        <span className="block text-sm font-medium">{label}</span>
        {description && <span className="mt-0.5 block text-xs text-[var(--color-muted)]">{description}</span>}
      </span>
      <span
        className={`relative h-7 w-12 shrink-0 rounded-full border transition ${
          checked ? "border-emerald-400/40 bg-emerald-500/30" : "border-white/10 bg-white/5"
        }`}
      >
        <span
          className={`absolute top-1 h-5 w-5 rounded-full bg-white transition-all ${checked ? "left-6" : "left-1"}`}
        />
      </span>
    </button>
  );
}

export function Row({ label, value, mono }: { label: ReactNode; value: ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-white/5 py-1.5 last:border-0">
      <span className="text-xs text-[var(--color-muted)]">{label}</span>
      <span className={`max-w-[62%] break-words text-right text-[13px] ${mono ? "mono" : ""}`}>{value}</span>
    </div>
  );
}

export function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: string }) {
  return (
    <div className="card p-3">
      <div className="text-[11px] uppercase tracking-wide text-[var(--color-muted)]">{label}</div>
      <div className={`mt-1 text-lg font-semibold ${tone ?? ""}`}>{value}</div>
      {hint && <div className="mt-0.5 text-xs text-[var(--color-muted)]">{hint}</div>}
    </div>
  );
}

export function Meter({ value, label, tone }: { value: number; label?: string; tone?: string }) {
  const clamped = Math.max(0, Math.min(100, value || 0));
  return (
    <div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-white/5">
        <div
          className={`h-full rounded-full ${tone ?? "bg-[var(--color-accent-2)]"}`}
          style={{ width: `${clamped}%` }}
        />
      </div>
      {label && <div className="mt-1 text-xs text-[var(--color-muted)]">{label}</div>}
    </div>
  );
}

export function Empty({ title, hint, action }: { title: string; hint?: ReactNode; action?: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-[var(--color-border)] p-4 text-center">
      <p className="text-sm font-medium">{title}</p>
      {hint && <p className="mt-1 text-xs text-[var(--color-muted)]">{hint}</p>}
      {action && <div className="mt-3 flex justify-center">{action}</div>}
    </div>
  );
}

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-3 text-sm text-[var(--color-muted)]">
      <span className="h-2 w-2 rounded-full bg-[var(--color-accent-2)] pulse" />
      {label}
    </div>
  );
}

export function ErrorNote({ message, action }: { message?: string | null; action?: ReactNode }) {
  if (!message) return null;
  return (
    <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">
      <p className="whitespace-pre-wrap">{message}</p>
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function InfoNote({ children, tone = "info" }: { children: ReactNode; tone?: "info" | "warn" | "ok" }) {
  const tones = {
    info: "border-sky-500/25 bg-sky-500/10 text-sky-100",
    warn: "border-amber-500/30 bg-amber-500/10 text-amber-100",
    ok: "border-emerald-500/30 bg-emerald-500/10 text-emerald-100",
  };
  return <div className={`rounded-xl border p-3 text-[13px] ${tones[tone]}`}>{children}</div>;
}

export function Sheet({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 sm:items-center" onClick={onClose}>
      <div
        className="safe-bottom max-h-[88vh] w-full overflow-y-auto rounded-t-2xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4 sm:max-w-lg sm:rounded-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="mb-3 flex items-center justify-between gap-3">
          <h3 className="text-base font-semibold">{title}</h3>
          <Button variant="ghost" onClick={onClose} className="!px-2">
            ✕
          </Button>
        </header>
        <div className="space-y-3">{children}</div>
        {footer && <div className="mt-4 flex justify-end gap-2">{footer}</div>}
      </div>
    </div>
  );
}

export function Toasts({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  const tones: Record<Toast["tone"], string> = {
    ok: "border-emerald-500/40 bg-emerald-500/15 text-emerald-100",
    warn: "border-amber-500/40 bg-amber-500/15 text-amber-100",
    error: "border-red-500/40 bg-red-500/15 text-red-100",
    info: "border-sky-500/40 bg-sky-500/15 text-sky-100",
  };
  return (
    <div className="pointer-events-none fixed inset-x-3 top-[calc(env(safe-area-inset-top)+0.5rem)] z-[60] flex flex-col gap-2 sm:inset-x-auto sm:right-4 sm:w-96">
      {toasts.map((toast) => (
        <button
          key={toast.id}
          onClick={() => onDismiss(toast.id)}
          className={`pointer-events-auto rounded-xl border px-3 py-2.5 text-left text-[13px] shadow-lg backdrop-blur ${tones[toast.tone]}`}
        >
          {toast.message}
        </button>
      ))}
    </div>
  );
}

export function Section({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <div className="space-y-2">
      <div>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-[var(--color-muted)]">{title}</h2>
        {hint && <p className="mt-0.5 text-xs text-[var(--color-muted)]">{hint}</p>}
      </div>
      {children}
    </div>
  );
}
