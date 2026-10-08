import { useState } from "react";
import { api } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { Badge, Button, Card, Empty, ErrorNote, InfoNote, Row, Section, Sheet, Spinner, Stat } from "../components/ui";
import { bytes } from "../lib/format";

const OPS = [
  { id: "doctor", label: "Run doctor", detail: "Upstream self-check of the Hermes install." },
  { id: "security-audit", label: "Security audit", detail: "Upstream audit of config and permissions." },
  { id: "backup", label: "Hermes backup", detail: "Creates a Hermes-side backup archive." },
  { id: "config-migrate", label: "Migrate config", detail: "Runs Hermes' config migration." },
  { id: "prompt-size", label: "Prompt size", detail: "Reports prompt/token footprint." },
  { id: "dump", label: "Diagnostic dump", detail: "Full diagnostic dump for bug reports." },
];

export default function Diagnostics({ notify }: { notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void }) {
  const checks = useAsync<any>(() => api.get("/api/cc/diagnostics"), [], 20000);
  const preflight = useAsync<any>(() => api.get("/api/cc/deploy/preflight").catch(() => null), [], 60000);
  const plan = useAsync<any>(() => api.get("/api/cc/runtime/bootstrap/plan").catch(() => null), [], 0);
  const capability = useAsync<any>(() => api.get("/api/cc/local/capability").catch(() => null), [], 60000);
  const [bootstrap, setBootstrap] = useState<any | null>(null);
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState<any | null>(null);

  const run = async (op: string) => {
    setBusy(op);
    setResult(null);
    try {
      const payload = await api.post<any>(`/api/cc/ops/${op}`);
      setResult({ op, payload });
      notify(`${op} finished.`, "ok");
    } catch (error) {
      notify((error as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const install = async () => {
    setBusy("bootstrap");
    try {
      const payload = await api.post<any>("/api/cc/runtime/bootstrap", { extras: "web", confirm: true });
      setBootstrap(payload);
      notify(payload.ok ? "Hermes dependencies installed." : "Install failed — see output.", payload.ok ? "ok" : "error");
    } catch (error) {
      notify((error as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const data = checks.data ?? {};
  const failing = (data.checks ?? []).filter((check: any) => !check.ok);

  return (
    <>
      <ErrorNote message={checks.error} />
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Checks passed" value={`${data.passed ?? 0}/${data.total ?? 0}`} />
        <Stat label="Blocking issues" value={failing.length} tone={failing.length ? "warn" : "ok"} />
        <Stat label="RAM" value={preflight.data?.host ? bytes(preflight.data.host.memory_total_bytes) : "—"} tone={(preflight.data?.host?.memory_total_bytes ?? 0) >= 2 * 1024 ** 3 ? "ok" : "warn"} />
        <Stat label="Disk free" value={preflight.data?.host ? bytes(preflight.data.host.disk_free_bytes) : "—"} />
      </div>

      <Card title="Preflight" subtitle="Will Hermes actually run on this host?" actions={<Button onClick={() => checks.reload()}>Re-check</Button>}>
        {checks.loading && !data.checks ? (
          <Spinner />
        ) : (
          <ul className="space-y-2">
            {(data.checks ?? []).map((check: any) => (
              <li key={check.id} className="rounded-xl border border-[var(--color-border)] p-2.5">
                <div className="flex items-start justify-between gap-2">
                  <span className="text-sm font-medium">{check.label}</span>
                  <Badge tone={check.ok ? "ok" : check.severity === "info" ? "info" : "error"}>{check.ok ? "PASS" : "FAIL"}</Badge>
                </div>
                <div className="mono mt-1 break-words text-[11px] text-[var(--color-muted)]">{check.detail}</div>
                {!check.ok && check.hint && <div className="mt-1 text-[11px] text-amber-300">{check.hint}</div>}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Runtime dependencies" subtitle="What runs when you press install">
        {plan.loading ? <Spinner /> : null}
        {plan.data && (
          <>
            <Row label="Interpreter" value={plan.data.interpreter || "not found"} mono />
            <Row label="Source" value={plan.data.source} mono />
            <Row label="Hermes home" value={plan.data.hermes_home} mono />
            <pre className="mt-2 max-h-40 overflow-auto rounded-lg bg-black/40 p-2 text-[11px]">
              {(plan.data.commands ?? []).join("\n")}
            </pre>
            <InfoNote tone="warn">
              The button below runs only the pip install line, in the detected Hermes interpreter. It downloads packages
              from PyPI. Nothing else is executed automatically.
            </InfoNote>
            <Button variant="primary" onClick={install} disabled={!plan.data.interpreter || busy === "bootstrap"}>
              {busy === "bootstrap" ? "Installing…" : "Install Hermes dependencies"}
            </Button>
          </>
        )}
        {bootstrap && (
          <pre className="mt-2 max-h-64 overflow-auto rounded-lg bg-black/40 p-2 text-[11px]">{bootstrap.output}</pre>
        )}
      </Card>

      <Card title="Local model capability" subtitle="Honest verdict for this host">
        {capability.loading ? <Spinner /> : null}
        {capability.data ? (
          <>
            <Row label="Verdict" value={capability.data.verdict?.state ?? "—"} />
            <Row label="Headline" value={capability.data.verdict?.headline ?? "—"} />
            <Row label="Recommendation" value={capability.data.verdict?.recommendation ?? "—"} />
            <Row label="Usable memory" value={bytes(capability.data.host?.memory_available_bytes)} />
            <Row label="Models that fit" value={(capability.data.verdict?.fits ?? []).length} />
            {capability.data.verdict?.detail && <InfoNote>{capability.data.verdict.detail}</InfoNote>}
            {capability.data.hermes?.error && <InfoNote tone="warn">{capability.data.hermes.error}</InfoNote>}
          </>
        ) : (
          <Empty title="Start the runtime to get a verdict" hint="Capability detection asks the Hermes runtime what it can run here." />
        )}
      </Card>

      <Card title="Maintenance" subtitle="Runs upstream Hermes operations through its API">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {OPS.map((op) => (
            <Button key={op.id} onClick={() => run(op.id)} disabled={busy === op.id}>
              <span className="block text-left">
                <span className="block">{busy === op.id ? "Running…" : op.label}</span>
                <span className="block text-[11px] font-normal text-[var(--color-muted)]">{op.detail}</span>
              </span>
            </Button>
          ))}
        </div>
        {result && (
          <pre className="mt-3 max-h-72 overflow-auto rounded-lg bg-black/40 p-2 text-[11px]">
            {JSON.stringify(result.payload, null, 2)}
          </pre>
        )}
      </Card>

      <Section title="Settings in force" hint="Environment as the Control Center sees it">
        <Card>
          <pre className="max-h-72 overflow-auto text-[11px]">{JSON.stringify(data.settings ?? {}, null, 2)}</pre>
        </Card>
      </Section>

      <Sheet open={Boolean(bootstrap && !bootstrap.ok)} title="Install output" onClose={() => setBootstrap(null)}>
        <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-words text-[11px]">{bootstrap?.output}</pre>
      </Sheet>
    </>
  );
}
