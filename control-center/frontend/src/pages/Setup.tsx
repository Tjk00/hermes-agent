import { useState } from "react";
import { api } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { bytes } from "../lib/format";
import { Badge, Button, Card, ErrorNote, InfoNote, Row, Sheet, Spinner, Stat } from "../components/ui";

const STEPS = [
  { id: "deployment", title: "How will you run this?", blurb: "Pick the shape that matches your hardware. Nothing here costs money." },
  { id: "model", title: "Choose a model", blurb: "Free routes first: the Nous free tier, a local model, or a provider free tier with your own free key." },
  { id: "providers", title: "Optional providers", blurb: "Skip freely. Paid providers only ever activate with a key you enter yourself." },
  { id: "admin", title: "Administrator", blurb: "Already created when you signed in — confirm the security defaults." },
  { id: "test", title: "System test", blurb: "Start the runtime and prove the agent answers before you rely on it." },
  { id: "launch", title: "Launch", blurb: "Finish and land on the dashboard." },
];

export default function Setup({ notify, onDone }: { notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void; onDone: () => void }) {
  const state = useAsync<any>(() => api.get("/api/cc/settings/setup"), [], 0);
  const deployment = useAsync<any>(() => api.get("/api/cc/deploy/platforms").catch(() => null), [], 0);
  const [index, setIndex] = useState(0);
  const [busy, setBusy] = useState("");
  const [payload, setPayload] = useState<Record<string, any>>({});
  const [testResult, setTestResult] = useState<any | null>(null);
  const [runtimePlan, setRuntimePlan] = useState<any | null>(null);

  const data = state.data ?? {};
  const steps: Record<string, any> = data.steps ?? {};
  const stepOrder: string[] = data.step_order ?? STEPS.map((item) => item.id);
  const completedIds = new Set(Object.entries(steps).filter(([, value]: any) => value?.completed).map(([key]) => key));
  const remaining = stepOrder.filter((id) => !completedIds.has(id)).length;
  const step = STEPS[index];
  const capability = data.capability;
  const install = data.install ?? {};
  const runtime = data.runtime ?? {};

  const record = async (id: string, extra: Record<string, any> = {}, completed = true) => {
    await api.post("/api/cc/settings/setup", { step: id, completed, data: extra });
    state.reload();
  };

  const next = async () => {
    setBusy("next");
    try {
      await record(step.id, payload);
      setPayload({});
      if (index + 1 >= STEPS.length) {
        const result = await api.post<any>("/api/cc/settings/setup", { step: "launch", completed: true, data: {} });
        notify("Setup complete. Welcome aboard.", "ok");
        onDone();
        return result;
      }
      setIndex(index + 1);
    } catch (error) {
      notify((error as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const startRuntime = async () => {
    setBusy("runtime");
    setTestResult(null);
    try {
      const result = await api.post<any>("/api/cc/runtime/start");
      setTestResult(result);
      notify(result?.state === "running" || result?.ok ? "Runtime started." : "Runtime start returned a status — see details.", result?.state === "running" || result?.ok ? "ok" : "warn");
      state.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const testChat = async () => {
    setBusy("chat");
    setTestResult(null);
    try {
      const result = await api.post<any>("/api/cc/chat/test", { prompt: "Reply with a single word: pong" });
      setTestResult(result);
      notify(result?.ok ? "The agent answered — you are live." : "The agent did not answer; the reason is below.", result?.ok ? "ok" : "warn");
    } catch (error) {
      notify((error as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const showBootstrapPlan = async () => {
    setBusy("plan");
    try {
      setRuntimePlan(await api.get<any>("/api/cc/runtime/bootstrap/plan"));
    } catch (error) {
      notify((error as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="mx-auto w-full max-w-2xl space-y-3 p-4">
      <Card title="First-run setup" subtitle={`Step ${index + 1} of ${STEPS.length}`}>
        <div className="flex gap-1">
          {STEPS.map((item, itemIndex) => (
            <span
              key={item.id}
              className={`h-1.5 flex-1 rounded-full ${completedIds.has(item.id) || itemIndex < index ? "bg-emerald-400/70" : itemIndex === index ? "bg-[var(--color-accent)]" : "bg-white/10"}`}
            />
          ))}
        </div>
        <ol className="mt-3 space-y-1">
          {STEPS.map((item, itemIndex) => (
            <li key={item.id}>
              <button
                className={`w-full rounded-lg px-2 py-1.5 text-left text-[13px] ${itemIndex === index ? "bg-white/10" : ""}`}
                onClick={() => setIndex(itemIndex)}
              >
                <span className="mr-2 text-[var(--color-muted)]">{completedIds.has(item.id) ? "✓" : itemIndex + 1}</span>
                {item.title}
              </button>
            </li>
          ))}
        </ol>
      </Card>

      <ErrorNote message={state.error} />
      {state.loading && !state.data && <Spinner />}
      {state.data && (
        <InfoNote>
          {remaining === 0
            ? "Every step is recorded — you can change any of it later from the pages on the left."
            : `${completedIds.size} of ${stepOrder.length} steps recorded. Each step saves as you go; nothing is sent anywhere.`}
        </InfoNote>
      )}

      <Card title={step.title} subtitle={step.blurb}>
        {index === 0 && (
          <>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Stat label="RAM" value={bytes(capability?.host?.memory_total_bytes)} />
              <Stat label="Disk" value={bytes(capability?.host?.disk_free_bytes)} />
              <Stat label="Hermes source" value={install.is_hermes_source ? "found" : "missing"} tone={install.is_hermes_source ? "ok" : "warn"} />
              <Stat label="Python" value={install.selected_python_version ?? "—"} tone={install.selected_python ? "ok" : "warn"} />
            </div>
            {!install.is_hermes_source && (
              <InfoNote tone="warn">
                The Hermes checkout was not found at the configured path. Set CC_HERMES_SOURCE to your clone of
                github.com/NousResearch/hermes-agent, then reload.
              </InfoNote>
            )}
            <div className="mt-3 space-y-2">
              {(deployment.data?.platforms ?? []).slice(0, 4).map((platform: any) => (
                <button
                  key={platform.name}
                  className={`w-full rounded-xl border p-2.5 text-left ${payload.mode === platform.name ? "border-[var(--color-accent)]" : "border-[var(--color-border)]"}`}
                  onClick={() => setPayload({ ...payload, mode: platform.name, suitable: platform.suitable })}
                >
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-[13px] font-medium">{platform.name}</span>
                    <Badge tone={platform.suitable === "yes" ? "ok" : platform.suitable === "partial" ? "warn" : "error"}>{platform.cost_label}</Badge>
                  </div>
                  <div className="text-[11px] text-[var(--color-muted)]">{platform.holds_24_7_agent}</div>
                </button>
              ))}
            </div>
          </>
        )}

        {index === 1 && (
          <>
            <InfoNote>
              {capability?.verdict?.headline ?? "Capability detection needs the runtime running; you can also choose a free cloud route."}
            </InfoNote>
            <div className="mt-2 space-y-2">
              {(data.free_options ?? []).map((option: any) => (
                <button
                  key={option.id}
                  className={`w-full rounded-xl border p-2.5 text-left ${payload.route === option.id ? "border-[var(--color-accent)]" : "border-[var(--color-border)]"}`}
                  onClick={() => setPayload({ ...payload, route: option.id, label: option.label })}
                >
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-[13px] font-medium">{option.label}</span>
                    <Badge tone="info">{option.cost_label}</Badge>
                  </div>
                  <div className="text-[11px] text-[var(--color-muted)]">{option.detail}</div>
                </button>
              ))}
            </div>
            <InfoNote tone="warn">
              {data.recommended_mode === "local"
                ? "This host can technically run a local model, but expect it to be slow on CPU and to download several GB."
                : "This host cannot comfortably run a local model — use the free tier or a provider's free tier instead."}
            </InfoNote>
          </>
        )}

        {index === 2 && (
          <>
            <Row label="Paid providers" value="optional — always" />
            <InfoNote>
              Provider and key management lives on the Providers and Keys pages, where every entry is labelled FREE,
              LIMITED FREE TIER or PAID. You can finish setup without adding a single key.
            </InfoNote>
          </>
        )}

        {index === 3 && (
          <>
            <Row label="Admin account" value="created with your first sign-in" />
            <Row label="Session cookie" value="HttpOnly, SameSite=Lax, Secure when served over HTTPS" />
            <Row label="CSRF" value="double-submit token required for every write" />
            <Row label="Rate limiting" value="on sign-in, key reveal and other sensitive routes" />
          </>
        )}

        {index === 4 && (
          <>
            <Row label="Runtime" value={`${runtime.state ?? "unknown"}${runtime.pid ? ` (pid ${runtime.pid})` : ""}`} />
            <div className="mt-2 flex flex-wrap gap-2">
              <Button variant="primary" onClick={startRuntime} disabled={busy === "runtime"}>
                {busy === "runtime" ? "Starting…" : "Start Hermes runtime"}
              </Button>
              <Button onClick={testChat} disabled={busy === "chat" || runtime.state !== "running"}>
                {busy === "chat" ? "Asking…" : "Send a real test prompt"}
              </Button>
              <Button onClick={showBootstrapPlan} disabled={busy === "plan"}>Install plan</Button>
            </div>
            {runtime.last_error_hint && <InfoNote tone="warn">{runtime.last_error_hint}</InfoNote>}
            {testResult && (
              <pre className="mt-2 max-h-64 overflow-auto rounded-lg bg-black/40 p-2 text-[11px]">{JSON.stringify(testResult, null, 2)}</pre>
            )}
          </>
        )}

        {index === 5 && (
          <>
            <Row label="Free mode" value={payload.free_mode === false ? "off" : "on"} />
            <InfoNote>
              You are done. The dashboard shows runtime state, model in use and true cost posture. Add a provider key
              whenever you want more capacity — the free route keeps working either way.
            </InfoNote>
          </>
        )}

        {index === 1 && capability?.verdict?.detail && <InfoNote>{capability.verdict.detail}</InfoNote>}

        <div className="mt-4 flex flex-wrap gap-2">
          <Button onClick={() => setIndex(Math.max(0, index - 1))} disabled={index === 0}>Back</Button>
          <Button variant="primary" onClick={next} disabled={busy === "next"}>
            {index + 1 === STEPS.length ? "Finish setup" : "Save and continue"}
          </Button>
          <Button onClick={onDone}>Skip setup for now</Button>
        </div>
      </Card>

      <Sheet open={Boolean(runtimePlan)} title="Install plan" onClose={() => setRuntimePlan(null)}>
        <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-words text-[11px]">
          {JSON.stringify(runtimePlan, null, 2)}
        </pre>
      </Sheet>
    </div>
  );
}
