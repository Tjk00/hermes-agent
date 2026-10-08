import { useAsync } from "../lib/hooks";
import { api } from "../lib/api";
import { bytes, duration, relativeTime, truncate } from "../lib/format";
import { Badge, Button, Card, CostChip, Empty, ErrorNote, InfoNote, Meter, Row, Section, Spinner, Stat } from "../components/ui";

type Status = any;

export default function Dashboard({
  navigate,
  notify,
}: {
  navigate: (route: string) => void;
  notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void;
}) {
  const status = useAsync<Status>(() => api.get("/api/cc/status"), [], 5000);
  const activity = useAsync<any>(() => api.get("/api/cc/activity?limit=12"), [], 15000);

  const data = status.data;
  const runtime = data?.runtime ?? {};
  const host = data?.host ?? {};
  const free = data?.free_mode ?? {};
  const providers = data?.providers ?? {};
  const scheduler = data?.scheduler ?? {};
  const hermes = data?.hermes?.status ?? {};

  const act = async (path: string, label: string) => {
    try {
      notify(`${label}…`, "info");
      await api.post(path);
      status.reload();
      notify(`${label} requested.`, "ok");
    } catch (error) {
      notify(`${label} failed: ${(error as Error).message}`, "error");
    }
  };

  const memoryPercent = host.memory_total_bytes
    ? ((host.memory_total_bytes - host.memory_available_bytes) / host.memory_total_bytes) * 100
    : 0;
  const diskPercent = host.disk_total_bytes ? (100 - (host.disk_free_bytes / host.disk_total_bytes) * 100) : 0;
  const activeModel = data?.model ?? {};

  return (
    <>
      <ErrorNote
        message={status.error ?? runtime.last_error}
        action={
          runtime.last_error ? (
            <div className="space-y-1 text-xs">
              <p className="whitespace-pre-wrap">{runtime.last_error_hint}</p>
              <Button variant="primary" onClick={() => act("/api/cc/runtime/start", "Starting runtime")}>
                Try starting the runtime
              </Button>
            </div>
          ) : undefined
        }
      />

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat
          label="Agent"
          value={runtime.healthy ? "Running" : (runtime.state ?? "unknown")}
          hint={runtime.pid ? `pid ${runtime.pid} · ${duration(runtime.uptime_seconds)}` : "not running"}
          tone={runtime.healthy ? "text-emerald-300" : "text-amber-300"}
        />
        <Stat label="Free mode" value={free.enabled ? "ON" : "OFF"} hint={free.allow_paid ? "paid fallback allowed" : "paid disabled"} />
        <Stat
          label="Providers"
          value={providers.configured ?? 0}
          hint={`${providers.free_capable ?? 0} free-capable`}
        />
        <Stat
          label="Schedules"
          value={scheduler.jobs ?? "—"}
          hint={scheduler.jobs ? "cron jobs on the runtime" : "none defined"}
        />
      </div>

      <Card
        title="Active model"
        subtitle={activeModel.provider ? `${activeModel.provider} · ${activeModel.model}` : "No model selected yet"}
        actions={
          <Button variant="primary" onClick={() => navigate("models")}>
            Change
          </Button>
        }
      >
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <CostChip billing={free.cost?.billing} label={free.cost?.label} />
          {activeModel.info?.effective_context_length ? (
            <Badge>{(activeModel.info.effective_context_length / 1000).toFixed(0)}K context</Badge>
          ) : null}
          {runtime.healthy ? null : <Badge tone="warn">runtime down</Badge>}
        </div>
        <p className="text-[13px] text-[var(--color-muted)]">
          {free.cost?.cost_notes ??
            "Pick a model on the Models page. Local and free-tier routes are listed first; paid providers stay locked until you unlock them."}
        </p>
      </Card>

      <Card
        title="Runtime"
        subtitle={`${runtime.mode ?? "dashboard"} mode · ${runtime.base_url ?? ""}`}
        actions={
          <div className="flex gap-2">
            <Button onClick={() => act("/api/cc/runtime/restart", "Restarting runtime")}>Restart</Button>
            {runtime.healthy ? (
              <Button variant="danger" onClick={() => act("/api/cc/runtime/stop", "Stopping runtime")}>
                Stop
              </Button>
            ) : (
              <Button variant="primary" onClick={() => act("/api/cc/runtime/start", "Starting runtime")}>
                Start
              </Button>
            )}
          </div>
        }
      >
        <Row label="State" value={<Badge tone={runtime.healthy ? "ok" : "warn"}>{runtime.state}</Badge>} />
        <Row label="Uptime" value={duration(runtime.uptime_seconds)} />
        <Row label="Restarts (auto)" value={runtime.restarts ?? 0} />
        <Row label="Hermes release" value={hermes.release_date ?? "—"} />
        <Row label="Commit" value={hermes.commit ?? data?.hermes?.install?.git_commit ?? "—"} mono />
        <Row label="Config version" value={hermes.config_version ?? "—"} />
        {runtime.last_error && <Row label="Last error" value={<span className="text-red-300">{runtime.last_error}</span>} />}
      </Card>

      <Card title="This host" subtitle="CPU, memory, storage — the numbers that decide free-mode limits">
        <div className="grid grid-cols-2 gap-3">
          <Meter value={host.cpu_percent ?? 0} label={`CPU ${host.cpu_percent?.toFixed(0) ?? "—"}% · ${host.cpu_count ?? "?"} cores`} />
          <Meter
            value={memoryPercent}
            label={`RAM ${bytes(host.memory_total_bytes)} · ${bytes(host.memory_available_bytes)} free`}
            tone={memoryPercent > 85 ? "bg-amber-400" : "bg-[var(--color-accent-2)]"}
          />
          <Meter value={diskPercent} label={`Disk ${bytes(host.disk_total_bytes)} · ${bytes(host.disk_free_bytes)} free`} />
          <div className="text-xs text-[var(--color-muted)]">
            <div>Load: {(host.load_avg ?? []).map((n: number) => n.toFixed(2)).join(" / ") || "—"}</div>
            <div>GPU: {host.gpu_name ?? "none detected"}</div>
          </div>
        </div>
      </Card>

      <Section title="Recent activity" hint="Control Center audit trail (sign-ins, key changes, runtime actions)">
        <Card>
          {activity.loading && !activity.data ? (
            <Spinner />
          ) : (activity.data?.audit ?? []).length === 0 ? (
            <Empty title="Nothing yet" hint="Actions you take here are logged for review." />
          ) : (
            <ul className="divide-y divide-white/5">
              {(activity.data?.audit ?? []).slice(0, 10).map((entry: any, index: number) => (
                <li key={index} className="flex items-start justify-between gap-2 py-2 text-[13px]">
                  <div className="min-w-0">
                    <div className="truncate font-medium">{entry.action.replaceAll("_", " ")}</div>
                    <div className="truncate text-xs text-[var(--color-muted)]">{truncate(entry.detail || entry.target, 90)}</div>
                  </div>
                  <span className="shrink-0 text-xs text-[var(--color-muted)]">{relativeTime(entry.ts)}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </Section>

      {runtime.state === "error" && (
        <InfoNote tone="warn">
          The runtime is not running, so pages that read live agent data will show an explanation instead of data. Use
          Diagnostics for a checklist and the exact fix.
        </InfoNote>
      )}
    </>
  );
}
