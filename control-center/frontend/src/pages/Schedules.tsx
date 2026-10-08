import { useState } from "react";
import { api } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { relativeTime } from "../lib/format";
import { Badge, Button, Card, CostChip, Empty, ErrorNote, InfoNote, Input, Row, Section, Sheet, Spinner } from "../components/ui";

const COMMON = [
  { label: "Every day at 08:00", value: "0 8 * * *" },
  { label: "Every hour", value: "0 * * * *" },
  { label: "Every 15 minutes", value: "*/15 * * * *" },
  { label: "Weekdays at 09:30", value: "30 9 * * 1-5" },
  { label: "Every Sunday at 20:00", value: "0 20 * * 0" },
];

export default function Schedules({ notify }: { notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void }) {
  const jobs = useAsync<any[]>(() => api.get("/api/cc/schedules"), [], 20000);
  const targets = useAsync<any>(() => api.get("/api/cc/schedules/delivery-targets"), [], 60000);
  const models = useAsync<any>(() => api.get("/api/cc/models"), [], 60000);
  const [open, setOpen] = useState(false);
  const [runsFor, setRunsFor] = useState<any | null>(null);
  const runs = useAsync<any>(() => (runsFor ? api.get(`/api/cc/schedules/${runsFor.id}/runs`) : Promise.resolve(null)), [runsFor?.id]);
  const [draft, setDraft] = useState({ name: "", schedule: "0 8 * * *", prompt: "", deliver: "local", provider: "", model: "" });

  const list = jobs.data ?? [];

  const create = async () => {
    try {
      await api.post("/api/cc/schedules", {
        name: draft.name,
        schedule: draft.schedule,
        prompt: draft.prompt,
        deliver: draft.deliver,
        provider: draft.provider || null,
        model: draft.model || null,
      });
      notify("Schedule created — it runs on the runtime, not in this browser tab.", "ok");
      setOpen(false);
      setDraft({ name: "", schedule: "0 8 * * *", prompt: "", deliver: "local", provider: "", model: "" });
      jobs.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const action = async (job: any, verb: string) => {
    try {
      await api.post(`/api/cc/schedules/${job.id}/${verb}`);
      notify(`${job.name || job.id} ${verb}.`, "ok");
      jobs.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const remove = async (job: any) => {
    if (!window.confirm(`Delete schedule "${job.name || job.id}"?`)) return;
    try {
      await api.del(`/api/cc/schedules/${job.id}`);
      notify("Schedule deleted.", "ok");
      jobs.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  return (
    <>
      <ErrorNote message={jobs.error} />
      <InfoNote>
        Schedules run inside the Hermes runtime process. If the host sleeps or scales to zero, they are missed — that is a
        hosting limitation, not a bug. A schedule that pins a paid model is refused while Free Mode is on.
      </InfoNote>

      <Card
        title="Scheduled tasks"
        subtitle={`${list.length} job(s)`}
        actions={<Button variant="primary" onClick={() => setOpen(true)}>New schedule</Button>}
      >
        {jobs.loading && !list.length ? (
          <Spinner />
        ) : list.length === 0 ? (
          <Empty title="No schedules yet" hint="Daily reports, nightly cleanups, weekly audits — anything Hermes can do, on a timer." />
        ) : (
          <ul className="space-y-2">
            {list.map((job: any) => (
              <li key={job.id} className="rounded-xl border border-[var(--color-border)] p-2.5">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{job.name || job.prompt?.slice(0, 60) || job.id}</div>
                    <div className="mono text-xs text-[var(--color-muted)]">{job.schedule}</div>
                    {job.prompt && <div className="line-clamp-2 text-xs text-[var(--color-muted)]">{job.prompt}</div>}
                  </div>
                  <Badge tone={job.paused ? "warn" : "ok"}>{job.paused ? "PAUSED" : "ACTIVE"}</Badge>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-[var(--color-muted)]">
                  <span>delivers to {job.deliver ?? "local"}</span>
                  {job.model && <span>· {job.provider}/{job.model}</span>}
                  {job.model && (
                    <CostChip billing={job.billing || (job.provider_is_paid ? "paid" : "free_tier")} />
                  )}
                  {job.last_run && <span>· last run {relativeTime(job.last_run)}</span>}
                  {job.next_run && <span>· next {relativeTime(job.next_run).replace(" ago", "")}</span>}
                </div>
                <div className="mt-2 flex flex-wrap gap-2">
                  <Button onClick={() => action(job, job.paused ? "resume" : "pause")} className="!py-1.5 !text-xs">
                    {job.paused ? "Resume" : "Pause"}
                  </Button>
                  <Button onClick={() => action(job, "trigger")} className="!py-1.5 !text-xs">Run now</Button>
                  <Button onClick={() => setRunsFor(job)} className="!py-1.5 !text-xs">History</Button>
                  <Button variant="danger" onClick={() => remove(job)} className="!py-1.5 !text-xs">Delete</Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Delivery targets" subtitle="Where a schedule can send its result">
        <ul className="space-y-1">
          {((targets.data as any)?.targets ?? []).map((target: any) => (
            <li key={target.id ?? target.name} className="flex items-center justify-between gap-2 py-1 text-[13px]">
              <span className="truncate">{target.label ?? target.name}</span>
              <Badge tone={target.configured === false ? "neutral" : "ok"}>{target.configured === false ? "not configured" : "ready"}</Badge>
            </li>
          ))}
          {!(targets.data as any)?.targets?.length && <Empty title="Only local delivery is available" hint="Connect Telegram, Discord or Slack in the Hermes CLI to deliver elsewhere." />}
        </ul>
      </Card>

      <Sheet open={open} title="New schedule" onClose={() => setOpen(false)} footer={<>
        <Button onClick={() => setOpen(false)}>Cancel</Button>
        <Button variant="primary" onClick={create} disabled={!draft.schedule || !draft.prompt}>Create</Button>
      </>}>
        <Input label="Name" value={draft.name} onChange={(value) => setDraft({ ...draft, name: value })} placeholder="Morning briefing" />
        <Input label="Schedule (cron)" value={draft.schedule} onChange={(value) => setDraft({ ...draft, schedule: value })} hint="Five-field cron. Examples below." />
        <div className="flex flex-wrap gap-2">
          {COMMON.map((option) => (
            <Button key={option.value} onClick={() => setDraft({ ...draft, schedule: option.value })} className="!py-1 !text-xs">
              {option.label}
            </Button>
          ))}
        </div>
        <label className="block">
          <span className="mb-1 block text-xs text-[var(--color-muted)]">Prompt</span>
          <textarea
            className="min-h-[120px] w-full rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-2)] p-3 text-[13px] outline-none"
            value={draft.prompt}
            onChange={(event) => setDraft({ ...draft, prompt: event.target.value })}
            placeholder="Summarise my unread messages and post the list here."
          />
        </label>
        <Input label="Deliver to" value={draft.deliver} onChange={(value) => setDraft({ ...draft, deliver: value })} hint="local keeps the output in Hermes; platform ids deliver elsewhere." />
        <Input
          label="Pin to model (optional)"
          value={draft.model}
          onChange={(value) => setDraft({ ...draft, model: value })}
          hint="Leave empty to inherit your default model — safest for Free Mode."
        />
        {draft.model && (
          <InfoNote tone="warn">
            Pinning a model to a schedule routes spending there. Paid providers are rejected unless you unlocked paid
            fallback.
          </InfoNote>
        )}
        {(models.data?.policy?.allow_paid_fallback ?? false) === false && (
          <div className="text-xs text-[var(--color-muted)]">
            Free Mode is on: this schedule will use your free default or the runtime's free-first chain.
          </div>
        )}
      </Sheet>

      <Sheet open={Boolean(runsFor)} title={`Runs — ${runsFor?.name ?? ""}`} onClose={() => setRunsFor(null)}>
        {runs.loading ? <Spinner /> : null}
        <ul className="space-y-2">
          {((runs.data as any)?.runs ?? []).map((run: any, index: number) => (
            <li key={index} className="rounded-xl border border-[var(--color-border)] p-2.5 text-[13px]">
              <div className="flex items-center justify-between gap-2">
                <span>{run.status ?? "unknown"}</span>
                <span className="text-xs text-[var(--color-muted)]">{relativeTime(run.started_at ?? run.at)}</span>
              </div>
              {run.error && <div className="mt-1 text-xs text-red-300">{run.error}</div>}
              {run.summary && <div className="mt-1 line-clamp-3 text-xs text-[var(--color-muted)]">{run.summary}</div>}
            </li>
          ))}
          {!((runs.data as any)?.runs ?? []).length && <Empty title="No runs recorded yet" />}
        </ul>
      </Sheet>

      <Section title="Notes" hint="What schedules inherit">
        <Card>
          <Row label="Runtime required" value="yes — schedules execute inside the agent process" />
          <Row label="Missed runs" value="skipped while the host is asleep or scaled to zero" />
          <Row label="Paid guard" value="automatic: paid providers need an explicit unlock" />
        </Card>
      </Section>
    </>
  );
}
