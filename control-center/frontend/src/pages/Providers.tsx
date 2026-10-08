import { useState } from "react";
import { api } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { Badge, Button, Card, CostChip, Empty, ErrorNote, InfoNote, Input, Row, Section, Sheet, Spinner, Toggle } from "../components/ui";

export default function Providers({ navigate, notify }: { navigate: (route: string) => void; notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void }) {
  const providers = useAsync<any>(() => api.get("/api/cc/providers"), [], 30000);
  const freeMode = useAsync<any>(() => api.get("/api/cc/free-mode"), [], 30000);
  const spend = useAsync<any>(() => api.get("/api/cc/providers/spend-guard"), [], 30000);
  const endpoints = useAsync<any>(() => api.get("/api/cc/providers/custom-endpoints"), [], 30000);

  const [active, setActive] = useState<any | null>(null);
  const [keyValue, setKeyValue] = useState("");
  const [keyName, setKeyName] = useState("");
  const [testing, setTesting] = useState(false);
  const [endpoint, setEndpoint] = useState({ name: "Ollama", base_url: "http://127.0.0.1:11434/v1", model: "" });
  const [showEndpoint, setShowEndpoint] = useState(false);

  const list = providers.data?.providers ?? [];
  const counts = providers.data?.counts ?? {};
  const free = freeMode.data ?? {};

  const saveKey = async () => {
    if (!active) return;
    const name = keyName || active.keys?.[0]?.name;
    if (!name) {
      notify("This provider has no known environment variable to store a key under.", "warn");
      return;
    }
    try {
      const result = await api.put<any>(`/api/cc/keys/${name}`, { value: keyValue, validate_with_provider: true });
      if (!result.stored) {
        notify(result.message ?? "The provider rejected this key (not saved).", "error");
        return;
      }
      notify(`${name} saved (encrypted)${result.validation?.ok ? " and validated" : ""}. Restart the runtime to apply.`, "ok");
      setKeyValue("");
      providers.reload();
      freeMode.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const removeKey = async (name: string) => {
    if (!window.confirm(`Remove ${name}?`)) return;
    try {
      await api.del(`/api/cc/keys/${name}`);
      notify(`${name} removed.`, "ok");
      providers.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const validateStored = async () => {
    if (!active) return;
    const name = keyName || active.keys?.[0]?.name;
    if (!name) return;
    setTesting(true);
    try {
      const value = (await api.get<{ keys: any[] }>("/api/cc/keys")).keys.find((item) => item.name === name);
      if (!value?.configured) {
        notify(`${name} is not configured yet.`, "warn");
        return;
      }
      const revealed = await api.post<{ value: string }>(`/api/cc/keys/${name}/reveal`);
      const result = await api.post<any>("/api/cc/providers/validate", { key: name, value: revealed.value });
      notify(result.ok ? "Provider accepted the key." : `Provider rejected the key: ${result.error}`, result.ok ? "ok" : "error");
    } catch (error) {
      notify((error as Error).message, "error");
    } finally {
      setTesting(false);
    }
  };

  const togglePaid = async (next: boolean) => {
    if (next && !window.confirm("Allow paid models to be selected and used in fallbacks? This can cost money.")) return;
    try {
      await api.put("/api/cc/free-mode", { allow_paid_fallback: next, confirm_paid: next });
      notify(next ? "Paid fallback UNLOCKED — paid models may now be used." : "Paid fallback locked.", next ? "warn" : "ok");
      freeMode.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const toggleFreeTier = async (next: boolean) => {
    try {
      const result = await api.put<any>("/api/cc/free-mode/nous-free-tier", { enabled: next });
      notify(result.note ?? "Free tier updated.", "info");
      if (next) {
        await api.post("/api/cc/runtime/restart");
        notify("Runtime restarting to apply the free tier.", "info");
      }
      freeMode.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const registerEndpoint = async () => {
    try {
      await api.post("/api/cc/providers/custom-endpoints", { ...endpoint, discover_models: true, make_default: true });
      notify("Local endpoint registered — it appears as a provider now.", "ok");
      setShowEndpoint(false);
      providers.reload();
      freeMode.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const probeEndpoint = async () => {
    try {
      const result = await api.post<any>(`/api/cc/providers/probe?url=${encodeURIComponent(endpoint.base_url)}`);
      notify(result.detail + (result.hint ? ` ${result.hint}` : ""), result.reachable ? "ok" : "warn");
      if (result.models?.length && !endpoint.model) setEndpoint({ ...endpoint, model: result.models[0] });
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  return (
    <>
      <ErrorNote message={providers.error} />

      <Card title="Free Mode" subtitle="The core promise: nothing paid is used unless you unlock it">
        <div className="grid gap-2">
          <Toggle
            label="Free Mode"
            description="Paid providers are excluded from selection, fallback chains and schedules."
            checked={Boolean(free.free_mode)}
            onChange={async (next) => {
              await api.put("/api/cc/free-mode", { free_mode: next });
              notify(next ? "Free Mode on." : "Free Mode off — paid models still need an explicit unlock.", "info");
              freeMode.reload();
            }}
          />
          <Toggle
            label="Allow paid models"
            description="Explicit unlock. Required before any paid model can be selected or scheduled."
            checked={Boolean(free.allow_paid_fallback)}
            onChange={togglePaid}
          />
          <Toggle
            label="Nous free tier (no API key)"
            description="Sets HERMES_GUEST_ONBOARDING for the runtime. Availability is gated by Nous and can change."
            checked={Boolean(free.nous_free_tier_enabled)}
            onChange={toggleFreeTier}
          />
        </div>
        <div className="mt-3 rounded-xl border border-[var(--color-border)] p-3 text-[13px]">
          <div className="mb-1 flex items-center gap-2">
            <span className="text-xs uppercase tracking-wide text-[var(--color-muted)]">Active route</span>
            <CostChip billing={free.current_cost?.billing} label={free.status?.label} />
          </div>
          <div className="text-[var(--color-muted)]">
            {free.status?.provider ? `${free.status.provider}/${free.status.model}` : "no model selected"} ·{" "}
            {free.status?.cost}
          </div>
          <p className="mt-1 text-xs text-[var(--color-muted)]">{free.status?.honest_note}</p>
        </div>
      </Card>

      <Card title="What could cost money" subtitle="Configured ≠ active. This is what is configured.">
        {(spend.data?.paid_configured_not_necessarily_active ?? []).length === 0 ? (
          <Empty title="No paid provider configured" hint="Nothing here can charge you right now." />
        ) : (
          <ul className="space-y-2">
            {(spend.data?.could_cost_money ?? []).map((item: any) => (
              <li key={item.provider} className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-2.5 text-[13px]">
                <div className="font-medium">{item.label}</div>
                <div className="text-xs text-[var(--color-muted)]">{item.why}</div>
                {item.verify && (
                  <a className="text-xs text-[var(--color-accent-2)] underline" href={item.verify} target="_blank" rel="noreferrer">
                    verify pricing
                  </a>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Section title="Providers" hint={`${counts.configured ?? 0} configured · ${counts.free_local ?? 0} local · ${counts.free_tier ?? 0} free tier · ${counts.paid ?? 0} paid · ${counts.unknown ?? 0} unverified`}>
        {providers.loading && !list.length ? <Spinner /> : null}
        <div className="grid gap-2 sm:grid-cols-2">
          {list.map((provider: any) => (
            <Card key={provider.id} title={provider.label} subtitle={provider.id}>
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <CostChip billing={provider.billing} label={provider.cost_label} />
                <Badge tone={provider.authenticated ? "ok" : "neutral"}>{provider.authenticated ? "CONFIGURED" : "NOT CONFIGURED"}</Badge>
                {provider.local && <Badge tone="info">LOCAL</Badge>}
                {provider.model_count ? <Badge>{provider.model_count} models</Badge> : null}
              </div>
              <p className="mb-2 line-clamp-3 text-xs text-[var(--color-muted)]">{provider.cost_notes}</p>
              <div className="flex flex-wrap gap-2">
                <Button
                  onClick={() => {
                    setActive(provider);
                    setKeyName(provider.keys?.[0]?.name ?? "");
                    setKeyValue("");
                  }}
                >
                  {provider.authenticated ? "Manage" : "Add key"}
                </Button>
                {provider.keys?.length ? (
                  <Button
                    variant="danger"
                    onClick={() => removeKey(provider.keys.find((key: any) => key.stored_in_control_center)?.name ?? provider.keys[0].name)}
                    disabled={!provider.keys.some((key: any) => key.stored_in_control_center)}
                  >
                    Remove key
                  </Button>
                ) : null}
              </div>
            </Card>
          ))}
        </div>
      </Section>

      <Card title="Local / free endpoints" subtitle="Ollama, LM Studio, llama.cpp, vLLM — anything OpenAI-compatible" actions={<Button onClick={() => setShowEndpoint(true)}>Register endpoint</Button>}>
        {Array.isArray(endpoints.data?.endpoints) && endpoints.data.endpoints.length === 0 && (
          <Empty title="No custom endpoints" hint="Add one to use $0 local inference through the Hermes runtime." />
        )}
        <ul className="space-y-1.5">
          {(Array.isArray(endpoints.data?.endpoints) ? endpoints.data.endpoints : []).map((item: any, index: number) => (
            <li key={index} className="rounded-xl border border-[var(--color-border)] p-2.5 text-[13px]">
              <div className="font-medium">{item.name ?? item.id}</div>
              <div className="mono truncate text-xs text-[var(--color-muted)]">{item.base_url}</div>
            </li>
          ))}
        </ul>
      </Card>

      <Sheet
        open={Boolean(active)}
        title={active?.label ?? ""}
        onClose={() => setActive(null)}
        footer={<Button onClick={() => setActive(null)}>Done</Button>}
      >
        {active && (
          <>
            <CostChip billing={active.billing} label={active.cost_label} />
            <p className="text-[13px] text-[var(--color-muted)]">{active.cost_notes}</p>
            <Row label="Requires a key" value={active.requires_key ? "yes" : "no"} />
            <Row label="Card required" value={active.credit_card_required === null ? "unverified" : active.credit_card_required ? "yes" : "no"} />
            {active.oauth?.available && (
              <>
                <Row label="OAuth sign-in" value={active.oauth.logged_in ? "signed in" : "available"} />
                {active.oauth.cli_command && <Row label="CLI command" value={active.oauth.cli_command} mono />}
              </>
            )}
            {active.keys?.length ? (
              <div className="space-y-2">
                {active.keys.map((key: any) => (
                  <div key={key.name} className="rounded-xl border border-[var(--color-border)] p-2.5">
                    <div className="flex items-center justify-between gap-2">
                      <span className="mono text-xs">{key.name}</span>
                      <Badge tone={key.is_set ? "ok" : "neutral"}>{key.is_set ? "SET" : "not set"}</Badge>
                    </div>
                    <div className="mt-1 text-[11px] text-[var(--color-muted)]">{key.description}</div>
                    {key.masked && <div className="mono mt-1 text-[11px]">{key.masked}</div>}
                    {key.url && (
                      <a className="text-[11px] text-[var(--color-accent-2)] underline" href={key.url} target="_blank" rel="noreferrer">
                        where to get this
                      </a>
                    )}
                  </div>
                ))}
                <Input
                  label={`Value for ${keyName || active.keys[0].name}`}
                  value={keyValue}
                  onChange={setKeyValue}
                  type="password"
                  placeholder="paste the key — it is stored encrypted on the server"
                />
                <div className="flex flex-wrap gap-2">
                  <Button variant="primary" onClick={saveKey} disabled={!keyValue.trim()}>
                    Save key
                  </Button>
                  <Button onClick={validateStored} disabled={testing}>
                    {testing ? "Testing…" : "Test stored key"}
                  </Button>
                </div>
                <InfoNote>
                  Keys are encrypted at rest and injected into the Hermes process environment at start. They are never
                  written into the repository, never sent to the browser, and masked in every log line.
                </InfoNote>
              </div>
            ) : (
              <InfoNote tone="warn">
                This provider is configured outside the Control Center (OAuth sign-in or config file). Use the Hermes CLI
                for its credentials, or add a key under another provider.
              </InfoNote>
            )}
            <Button variant="ghost" onClick={() => navigate("keys")}>
              Open the key manager →
            </Button>
          </>
        )}
      </Sheet>

      <Sheet
        open={showEndpoint}
        title="Register a local endpoint"
        onClose={() => setShowEndpoint(false)}
        footer={
          <>
            <Button onClick={probeEndpoint}>Test connection</Button>
            <Button variant="primary" onClick={registerEndpoint} disabled={!endpoint.model}>
              Register
            </Button>
          </>
        }
      >
        <Input label="Name" value={endpoint.name} onChange={(value) => setEndpoint({ ...endpoint, name: value })} />
        <Input
          label="Base URL"
          value={endpoint.base_url}
          onChange={(value) => setEndpoint({ ...endpoint, base_url: value })}
          hint="Ollama: http://127.0.0.1:11434/v1 · LM Studio: http://127.0.0.1:1234/v1"
          inputMode="url"
        />
        <Input
          label="Model id"
          value={endpoint.model}
          onChange={(value) => setEndpoint({ ...endpoint, model: value })}
          hint="Press Test connection to list the models the server offers."
        />
        <InfoNote tone="ok">
          Local inference is FREE: $0 per token, no account, no key. It uses your own CPU/GPU and is only as fast as this
          machine.
        </InfoNote>
      </Sheet>
    </>
  );
}
