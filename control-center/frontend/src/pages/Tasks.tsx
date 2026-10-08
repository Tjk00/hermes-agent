import { useAsync } from "../lib/hooks";
import { api } from "../lib/api";
import { duration, relativeTime, truncate } from "../lib/format";
import { Badge, Button, Card, Empty, ErrorNote, InfoNote, Row, Section, Spinner, Stat } from "../components/ui";

export default function Tasks({ notify }: { notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void }) {
  const events = useAsync<any>(() => api.get("/api/cc/activity?limit=80"), [], 15000);
  const stats = useAsync<any>(() => api.get("/api/cc/sessions/stats").catch(() => null), [], 60000);
  const runtime = useAsync<any>(() => api.get("/api/cc/runtime"), [], 10000);
  const vendors = useAsync<any>(() => api.get("/api/cc/analytics/usage").catch(() => null), [], 60000);

  const runtimeEvents = events.data?.runtime_events ?? [];
  const audit = events.data?.audit ?? [];

  const refresh = () => {
    events.reload();
    stats.reload();
    runtime.reload();
    vendors.reload();
    notify("Activity refreshed.", "ok");
  };

  return (
    <>
      <ErrorNote message={events.error} />
      <Card title="Activity" subtitle="Runtime events and audited actions" actions={<Button onClick={refresh}>Refresh</Button>} />
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Runtime" value={runtime.data?.runtime?.healthy ? "up" : "down"} hint={duration(runtime.data?.runtime?.uptime_seconds)} />
        <Stat label="Sessions" value={stats.data?.total ?? stats.data?.sessions ?? "—"} hint="recorded by Hermes" />
        <Stat label="Runtime events" value={runtimeEvents.length} hint="start/stop/crash trail" />
        <Stat label="Audit entries" value={audit.length} hint="Control Center actions" />
      </div>

      <Card title="Runtime event trail" subtitle="Every lifecycle transition the supervisor recorded" actions={<Button onClick={() => events.reload()}>Refresh</Button>}>
        {events.loading && !runtimeEvents.length ? (
          <Spinner />
        ) : runtimeEvents.length === 0 ? (
          <Empty title="No runtime events yet" />
        ) : (
          <ul className="divide-y divide-white/5">
            {runtimeEvents.map((event: any, index: number) => (
              <li key={index} className="flex items-start justify-between gap-2 py-2 text-[13px]">
                <div className="min-w-0">
                  <div className="font-medium">{event.event.replaceAll("_", " ")}</div>
                  {event.detail && <div className="truncate text-xs text-[var(--color-muted)]">{truncate(event.detail, 110)}</div>}
                </div>
                <span className="shrink-0 text-xs text-[var(--color-muted)]">{relativeTime(event.ts)}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {vendors.data && (
        <Card title="Model usage" subtitle="Reported by the runtime">
          <pre className="max-h-64 overflow-auto text-xs">{JSON.stringify(vendors.data, null, 2)}</pre>
        </Card>
      )}

      <Section title="Background work" hint="Where it actually runs">
        <Card>
          <Row label="Scheduler" value="inside the Hermes runtime process" />
          <Row label="This browser" value="never required — closing the tab does not stop work" />
          <Row label="Free-tier hosts" value="scale-to-zero hosts miss scheduled runs (see Deployment)" />
          <Row label="Watching it" value="Logs → source: runtime, and Schedules → History" />
        </Card>
      </Section>

      <Card title="Control Center actions" subtitle="Audit trail">
        <ul className="divide-y divide-white/5">
          {audit.slice(0, 30).map((entry: any, index: number) => (
            <li key={index} className="flex items-start justify-between gap-2 py-2 text-[13px]">
              <div className="min-w-0">
                <div className="truncate">{entry.action.replaceAll("_", " ")} <span className="text-xs text-[var(--color-muted)]">by {entry.actor}</span></div>
                {entry.detail && <div className="truncate text-xs text-[var(--color-muted)]">{truncate(entry.detail, 100)}</div>}
              </div>
              <Badge tone={entry.level === "warning" ? "warn" : "neutral"}>{entry.level}</Badge>
            </li>
          ))}
        </ul>
      </Card>

      <InfoNote>
        “Tasks” in Hermes are the agent's own turns and scheduled runs. Long-running work is delegated to the runtime, so
        you can close this page and the work continues — provided the host stays awake.
      </InfoNote>
    </>
  );
}
