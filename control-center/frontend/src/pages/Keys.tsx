import { useState } from "react";
import { api } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { relativeTime } from "../lib/format";
import { Badge, Button, Card, CostChip, Empty, ErrorNote, InfoNote, Input, Row, Spinner, Toggle } from "../components/ui";

export default function Keys({ notify }: { notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void }) {
  const providers = useAsync<any>(() => api.get("/api/cc/providers"), [], 30000);
  const stored = useAsync<any>(() => api.get("/api/cc/keys"), [], 20000);
  const settings = useAsync<any>(() => api.get("/api/cc/settings"), [], 30000);
  const [draft, setDraft] = useState({ name: "", value: "" });

  const rows: any[] = [];
  for (const provider of providers.data?.providers ?? []) {
    for (const key of provider.keys ?? []) {
      rows.push({ ...key, provider: provider.label, providerId: provider.id, billing: provider.billing, costLabel: provider.cost_label });
    }
  }
  const providerKeys = rows.filter((row) => row.is_password || row.category === "provider");
  const otherKeys = rows.filter((row) => !(row.is_password || row.category === "provider") && row.is_set);

  const save = async () => {
    if (!draft.name.trim()) return;
    try {
      const result = await api.put<any>(`/api/cc/keys/${draft.name.trim()}`, { value: draft.value, validate_with_provider: true });
      if (!result.stored) {
        notify(result.message ?? "The provider rejected that key — nothing was stored.", "error");
        return;
      }
      notify(`${draft.name} stored (encrypted)${result.validation?.ok ? " and validated" : ""}.`, "ok");
      setDraft({ name: "", value: "" });
      stored.reload();
      providers.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const remove = async (name: string) => {
    if (!window.confirm(`Delete stored key ${name}?`)) return;
    try {
      await api.del(`/api/cc/keys/${name}`);
      notify(`${name} deleted.`, "ok");
      stored.reload();
      providers.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const reveal = async (name: string) => {
    if (!window.confirm(`Reveal ${name}? The reveal is written to the audit log.`)) return;
    try {
      const result = await api.post<{ value: string }>(`/api/cc/keys/${name}/reveal`);
      window.prompt("Copy the key now (it will not be shown again):", result.value);
      notify("Key revealed — the action is in the audit log.", "warn");
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  return (
    <>
      <ErrorNote message={stored.error ?? providers.error} />

      <Card title="Add or replace a key" subtitle="Optional — paid providers stay off until you add your own key">
        <div className="grid gap-2">
          <Input
            label="Environment variable"
            value={draft.name}
            onChange={(value) => setDraft({ ...draft, name: value.toUpperCase() })}
            placeholder="OPENAI_API_KEY"
            hint="Use the exact name the provider expects (the list below shows every name Hermes recognises)."
          />
          <Input label="Key value" type="password" value={draft.value} onChange={(value) => setDraft({ ...draft, value })} placeholder="paste once — stored encrypted" />
          <Button variant="primary" onClick={save} disabled={!draft.name.trim() || !draft.value.trim()}>
            Store key
          </Button>
        </div>
      </Card>

      <Card title="Stored in the Control Center" subtitle={stored.data?.cipher ? `Cipher: ${stored.data.cipher}` : undefined}>
        {(stored.data?.keys ?? []).length === 0 ? (
          <Empty title="No keys stored yet" hint="Add one above, or use the free tier / a local model so no key is needed at all." />
        ) : (
          <ul className="space-y-2">
            {(stored.data?.keys ?? []).map((key: any) => (
              <li key={key.name} className="rounded-xl border border-[var(--color-border)] p-2.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="mono truncate text-[13px]">{key.name}</span>
                  <Badge tone="ok">{key.configured ? "CONFIGURED" : "empty"}</Badge>
                </div>
                <div className="mono mt-1 text-xs text-[var(--color-muted)]">{key.masked}</div>
                <div className="mt-1 text-[11px] text-[var(--color-muted)]">
                  updated {relativeTime(key.updated_at)} {key.updated_by ? `by ${key.updated_by}` : ""}
                </div>
                <div className="mt-2 flex gap-2">
                  <Button onClick={() => reveal(key.name)} className="!py-1.5 !text-xs">Reveal</Button>
                  <Button variant="danger" onClick={() => remove(key.name)} className="!py-1.5 !text-xs">Delete</Button>
                </div>
              </li>
            ))}
          </ul>
        )}
        <div className="mt-3">
          <Toggle
            label="Also write keys to $HERMES_HOME/.env"
            description="Off by default. Turning this on makes the Hermes CLI see the keys, but stores them in plaintext on disk (mode 0600)."
            checked={Boolean(settings.data?.values?.sync_keys_to_hermes_env)}
            onChange={async (next) => {
              try {
                const result = await api.put<any>("/api/cc/keys/policy/sync-to-hermes-env", { enabled: next });
                notify(next ? result.warning : "Keys are injected into the runtime environment only.", next ? "warn" : "ok");
                settings.reload();
              } catch (error) {
                notify((error as Error).message, "error");
              }
            }}
          />
        </div>
        <div className="mt-3 space-y-1 text-xs text-[var(--color-muted)]">
          <Row label="Storage" value={stored.data?.policy?.storage ?? "—"} />
          <Row label="Injection" value={stored.data?.policy?.injection ?? "—"} />
          <Row label="Reveal policy" value={stored.data?.policy?.reveal ?? "—"} />
        </div>
      </Card>

      <Card title="Provider key slots" subtitle="Which providers expect which variables — and whether they are set">
        {providers.loading && !providerKeys.length ? (
          <Spinner />
        ) : (
          <ul className="space-y-2">
            {providerKeys.slice(0, 80).map((key) => (
              <li key={key.name} className="rounded-xl border border-[var(--color-border)] p-2.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="mono truncate text-xs">{key.name}</span>
                  <Badge tone={key.is_set ? "ok" : "neutral"}>{key.is_set ? "SET" : "NOT SET"}</Badge>
                </div>
                <div className="mt-1 flex items-center gap-2 text-[11px] text-[var(--color-muted)]">
                  <CostChip billing={key.billing} label={key.costLabel} />
                  <span className="truncate">{key.provider}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
        <InfoNote>
          A provider marked PAID requires you to pay that provider directly. A LIMITED FREE TIER means the provider
          documents a free allowance with limits — it can change, so the Control Center links to their pricing page.
        </InfoNote>
      </Card>

      {otherKeys.length > 0 && (
        <Card title="Other configured variables" subtitle="Platform tokens and tool keys Hermes has set">
          <ul className="space-y-1">
            {otherKeys.slice(0, 40).map((key) => (
              <li key={key.name} className="flex items-center justify-between gap-2 py-1 text-[13px]">
                <span className="mono truncate">{key.name}</span>
                <Badge tone="ok">SET</Badge>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </>
  );
}
