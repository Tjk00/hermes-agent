import { useState } from "react";
import { api } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { bytes } from "../lib/format";
import { Badge, Button, Card, ErrorNote, InfoNote, Row, Section, Spinner, Stat } from "../components/ui";

const TONE: Record<string, "ok" | "warn" | "error" | "info" | "neutral"> = {
  yes: "ok",
  partial: "warn",
  no: "error",
  FREE: "ok",
  "SELF-HOSTED": "info",
  "LIMITED FREE TIER": "warn",
  PAID: "error",
};

export default function Deploy() {
  const matrix = useAsync<any>(() => api.get("/api/cc/deploy/platforms"), [], 0);
  const freeHosting = useAsync<any>(() => api.get("/api/cc/deploy/free-hosting"), [], 0);
  const preflight = useAsync<any>(() => api.get("/api/cc/deploy/preflight"), [], 30000);
  const [showAll, setShowAll] = useState(false);

  const platforms = matrix.data?.platforms ?? [];
  const shown = showAll ? platforms : platforms.slice(0, 6);
  const host = preflight.data?.host ?? {};
  const verdict = preflight.data?.verdict ?? {};

  return (
    <>
      <ErrorNote message={matrix.error ?? preflight.error} />

      <Card title={freeHosting.data?.name ?? "FREE HOSTING MODE"} subtitle="What this app does to stay at $0">
        <p className="text-[13px] text-[var(--color-muted)]">{freeHosting.data?.what_it_does}</p>
        <ul className="mt-2 space-y-1">
          {(freeHosting.data?.requirements ?? []).map((item: string) => (
            <li key={item} className="flex gap-2 text-[13px]"><span>•</span><span>{item}</span></li>
          ))}
        </ul>
        {freeHosting.data?.what_it_does_not && (
          <InfoNote tone="warn">{freeHosting.data.what_it_does_not}</InfoNote>
        )}
      </Card>

      <Card title="This host" subtitle="Measured a moment ago, not guessed">
        {preflight.loading ? (
          <Spinner />
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Stat label="RAM" value={bytes(host.memory_total_bytes)} tone={(host.memory_total_bytes ?? 0) >= 2 * 1024 ** 3 ? "ok" : "warn"} hint={`${bytes(host.memory_available_bytes)} available`} />
              <Stat label="CPU" value={host.cpu_count ?? "—"} hint="logical cores" />
              <Stat label="Disk free" value={bytes(host.disk_free_bytes)} tone={(host.disk_free_bytes ?? 0) > 2e9 ? "ok" : "warn"} />
              <Stat label="Load" value={host.cpu_percent ?? "—"} hint={`${host.platform ?? ""}`} />
            </div>
            <Row label="Verdict" value={verdict.headline ?? "—"} />
            <div className="mt-2 space-y-2">
              {(preflight.data?.checks ?? []).map((check: any) => (
                <div key={check.id} className="rounded-xl border border-[var(--color-border)] p-2.5">
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-[13px] font-medium">{check.label}</span>
                    <Badge tone={check.ok ? "ok" : "warn"}>{check.ok ? "OK" : "ATTENTION"}</Badge>
                  </div>
                  <div className="mono break-words text-[11px] text-[var(--color-muted)]">{check.detail}</div>
                  {!check.ok && check.hint && <div className="mt-1 text-[11px] text-amber-300">{check.hint}</div>}
                </div>
              ))}
            </div>
            {verdict.detail && <InfoNote>{verdict.detail}</InfoNote>}
            <div className="mt-2 space-y-1">
              {(preflight.data?.notes ?? []).map((note: string) => (
                <div key={note} className="text-[12px] text-[var(--color-muted)]">⚠ {note}</div>
              ))}
            </div>
          </>
        )}
      </Card>

      <Card
        title="Where can this run for $0?"
        subtitle={`Written ${matrix.data?.verified ?? "—"} — verify any row before you rely on it`}
        actions={<Button onClick={() => setShowAll(!showAll)}>{showAll ? "Show fewer" : `Show all ${platforms.length}`}</Button>}
      >
        <ul className="space-y-2">
          {shown.map((platform: any) => (
            <li key={platform.name} className="rounded-xl border border-[var(--color-border)] p-3">
              <div className="flex items-start justify-between gap-2">
                <span className="text-sm font-medium">{platform.name}</span>
                <Badge tone={TONE[platform.cost_label] ?? "neutral"}>{platform.cost_label}</Badge>
              </div>
              {platform.card_required && <div className="mt-1 text-[11px] text-amber-300">Requires a payment method on file.</div>}
              <div className="mt-2 space-y-1 text-[12px]">
                <div className="flex justify-between gap-3"><span className="text-[var(--color-muted)]">24/7 agent</span><span className="text-right">{platform.holds_24_7_agent}</span></div>
                <div className="flex justify-between gap-3"><span className="text-[var(--color-muted)]">Sleeps / terminates?</span><span className="text-right">{platform.sleeps}</span></div>
                <div className="flex justify-between gap-3"><span className="text-[var(--color-muted)]">RAM</span><span className="text-right">{platform.ram}</span></div>
                <div className="flex justify-between gap-3"><span className="text-[var(--color-muted)]">Persistent storage</span><span className="text-right">{platform.persistent_storage ? "yes" : "no (state lost on redeploy)"}</span></div>
                <div className="flex justify-between gap-3"><span className="text-[var(--color-muted)]">Background processes</span><span className="text-right">{platform.background_processes ? "yes" : "no"}</span></div>
                <div className="flex justify-between gap-3"><span className="text-[var(--color-muted)]">Good for</span><span className="text-right">{platform.suitable_for}</span></div>
              </div>
              <p className="mt-2 text-[12px] text-[var(--color-muted)]">{platform.notes}</p>
              {platform.verify_url && (
                <a
                  className="mt-1 inline-block text-[11px] text-[var(--color-accent)] underline"
                  href={platform.verify_url}
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  verify limits →
                </a>
              )}
            </li>
          ))}
        </ul>
        <InfoNote tone="warn">{matrix.data?.disclaimer}</InfoNote>
      </Card>

      <Card title="Recommendations" subtitle="Pick the shape that matches what you want">
        <Section title="$0 and always-on">
          <ul className="space-y-1">
            {(matrix.data?.recommendation?.zero_cost_24_7 ?? []).map((item: string) => (
              <li key={item} className="text-[13px]">• {item}</li>
            ))}
          </ul>
        </Section>
        <Section title="UI hosted free, runtime at home">
          <ul className="space-y-1">
            {(matrix.data?.recommendation?.zero_cost_ui_only ?? []).map((item: string) => (
              <li key={item} className="text-[13px]">• {item}</li>
            ))}
          </ul>
        </Section>
      </Card>

      <Card title="Docker" subtitle="One command, same result on any machine">
        <pre className="overflow-auto rounded-lg bg-black/40 p-3 text-[11px]">{`cd control-center
cp .env.example .env          # CC_* settings, no secrets required
docker compose up -d --build  # control center + api + hermes runtime (+ optional postgres)
# http://localhost:8080 → first-run wizard creates your admin account`}</pre>
        <Row label="Services" value="control-center, hermes (upstream runtime), optional postgres" />
        <Row label="Persistence" value="named volumes for CC_HOME and HERMES_HOME" />
        <Row label="Migration" value="docker compose down on the old host, copy the volumes, up on the new one" />
        <InfoNote>
          Docker can't be exercised from inside this build sandbox, so the compose file is provided and reviewed, not
          claimed as run-tested here. It uses only upstream images plus the local build context.
        </InfoNote>
      </Card>
    </>
  );
}
