import { useState } from "react";
import { api } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { Badge, Button, Card, Empty, ErrorNote, InfoNote, Input, Row, Sheet, Spinner } from "../components/ui";

export default function Memory({ notify }: { notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void }) {
  const memory = useAsync<any>(() => api.get("/api/cc/memory"), [], 30000);
  const [active, setActive] = useState<any | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});

  const providers = memory.data?.providers ?? [];
  const current = memory.data?.active ?? "";

  const openProvider = async (provider: any) => {
    setActive(provider);
    setValues({});
    try {
      const config = await api.get<any>(`/api/cc/memory/providers/${provider.name}/config`);
      const schema = config?.fields ?? config?.values ?? config ?? {};
      const next: Record<string, string> = {};
      for (const [key, value] of Object.entries(schema)) {
        next[key] = typeof value === "object" && value !== null ? String((value as any).value ?? "") : String(value ?? "");
      }
      setValues(next);
    } catch {
      setValues({});
    }
  };

  const activate = async (provider: string) => {
    try {
      await api.put("/api/cc/memory/provider", { provider });
      notify(provider ? `Memory provider set to ${provider}.` : "Memory provider cleared.", "ok");
      memory.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const saveConfig = async () => {
    if (!active) return;
    try {
      await api.put(`/api/cc/memory/providers/${active.name}/config`, { values });
      notify(`Saved configuration for ${active.name}.`, "ok");
      setActive(null);
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  return (
    <>
      <ErrorNote message={memory.error} />
      <InfoNote>
        Hermes' memory system keeps notes, user profile and skills between sessions. Providers marked “unavailable” need
        an external service or a CLI tool that is not installed — the Control Center reports what Hermes reports.
      </InfoNote>

      <Card title="Memory providers" subtitle={current ? `Active: ${current}` : "No external memory provider active (built-in memory still works)"}>
        {memory.loading && !providers.length ? (
          <Spinner />
        ) : providers.length === 0 ? (
          <Empty title="No memory providers reported" hint="The runtime may still be starting, or your build has none installed." />
        ) : (
          <ul className="space-y-2">
            {providers.map((provider: any) => (
              <li key={provider.name} className="rounded-xl border border-[var(--color-border)] p-2.5">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{provider.name}</div>
                    <div className="line-clamp-2 text-xs text-[var(--color-muted)]">{provider.description}</div>
                  </div>
                  <Badge tone={provider.status === "ready" || provider.status === "available" ? "ok" : provider.configured ? "warn" : "neutral"}>
                    {provider.status ?? (provider.configured ? "configured" : "not configured")}
                  </Badge>
                </div>
                <div className="mt-2 flex flex-wrap gap-2">
                  <Button onClick={() => activate(provider.name)} disabled={current === provider.name} className="!py-1.5 !text-xs">
                    {current === provider.name ? "Active" : "Activate"}
                  </Button>
                  <Button onClick={() => openProvider(provider)} className="!py-1.5 !text-xs">
                    Configure
                  </Button>
                </div>
                {provider.setup?.external_dependencies?.length ? (
                  <div className="mt-2 text-[11px] text-[var(--color-muted)]">
                    needs: {provider.setup.external_dependencies.map((dep: any) => dep.name).join(", ")}
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Danger zone" subtitle="These actions change the agent's remembered state">
        <div className="flex flex-wrap gap-2">
          <Button
            variant="danger"
            onClick={async () => {
              if (!window.confirm("Reset Hermes memory? Notes and profile data are cleared.")) return;
              try {
                await api.post("/api/cc/memory/reset", {});
                notify("Memory reset requested.", "warn");
              } catch (error) {
                notify((error as Error).message, "error");
              }
            }}
          >
            Reset memory
          </Button>
          <Button onClick={() => activate("")}>Disable external provider</Button>
        </div>
      </Card>

      <Sheet open={Boolean(active)} title={`${active?.name ?? ""} configuration`} onClose={() => setActive(null)} footer={<>
        <Button onClick={() => setActive(null)}>Cancel</Button>
        <Button variant="primary" onClick={saveConfig}>Save</Button>
      </>}>
        {Object.keys(values).length === 0 ? (
          <Empty title="Nothing to configure" hint="This provider has no editable fields, or it is configured elsewhere." />
        ) : (
          Object.entries(values).map(([key, value]) => (
            <Input key={key} label={key} value={value} onChange={(next) => setValues({ ...values, [key]: next })} />
          ))
        )}
        <Row label="Provider" value={active?.name ?? ""} />
      </Sheet>
    </>
  );
}
