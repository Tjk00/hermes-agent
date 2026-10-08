import { useState } from "react";
import { api } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { Badge, Button, Card, CostChip, Empty, ErrorNote, InfoNote, Input, Row, Sheet, Spinner } from "../components/ui";

export default function Models({ notify }: { notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void }) {
  const models = useAsync<any>(() => api.get("/api/cc/models"), [], 30000);
  const chain = useAsync<any>(() => api.get("/api/cc/models/chain"), [], 30000);
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<any | null>(null);
  const [confirmPaid, setConfirmPaid] = useState(false);

  const all = models.data?.models ?? [];
  const filtered = all.filter((item: any) =>
    `${item.provider} ${item.id}`.toLowerCase().includes(filter.toLowerCase()),
  );
  const byProvider = new Map<string, any[]>();
  for (const item of filtered) {
    const key = item.provider;
    byProvider.set(key, [...(byProvider.get(key) ?? []), item]);
  }
  const summary = models.data?.summary ?? {};
  const policy = models.data?.policy ?? {};

  const select = async (item: any) => {
    if (!item.free && !policy.allow_paid_fallback) {
      notify("That model is PAID. Unlock paid models in Providers → Free Mode first.", "warn");
      return;
    }
    try {
      const result = await api.post<any>("/api/cc/models/select", {
        provider: item.provider,
        model: item.id,
        scope: "global",
        confirm_expensive: confirmPaid,
      });
      notify(`Active model: ${item.provider}/${item.id} (${result.classification?.label})`, "ok");
      models.reload();
      setSelected(null);
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const test = async (item: any) => {
    try {
      const result = await api.post<any>("/api/cc/models/test", { provider: item.provider, model: item.id });
      notify(result.detail, result.available ? "ok" : "warn");
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const applyFreeChain = async () => {
    try {
      const result = await api.post<any>("/api/cc/models/free-chain/apply");
      notify(`Free chain applied: ${result.chain.map((hop: any) => hop.provider).join(" → ")}`, "ok");
      chain.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  return (
    <>
      <ErrorNote message={models.error} />
      <div className="grid grid-cols-3 gap-2">
        <Card title="Free" subtitle="local + free tier"><div className="text-2xl font-semibold text-emerald-300">{summary.free ?? 0}</div></Card>
        <Card title="Paid" subtitle="requires unlock"><div className="text-2xl font-semibold text-amber-300">{summary.paid ?? 0}</div></Card>
        <Card title="Total" subtitle="offerable now"><div className="text-2xl font-semibold">{summary.total ?? 0}</div></Card>
      </div>

      <Card
        title="Fallback chain"
        subtitle="Primary → secondary → … written into Hermes' own fallback_providers config"
        actions={<Button variant="primary" onClick={applyFreeChain}>Apply free-first chain</Button>}
      >
        {(chain.data?.chain_summary ?? []).length === 0 ? (
          <Empty
            title="No fallback chain yet"
            hint={
              chain.data?.recommended?.note ??
              "Apply the recommended free-first chain, or add hops manually below."
            }
          />
        ) : (
          <ul className="space-y-2">
            {chain.data.chain_summary.map((hop: any) => (
              <li key={hop.position} className="flex items-center justify-between gap-2 rounded-xl border border-[var(--color-border)] p-2.5">
                <div className="min-w-0">
                  <div className="truncate text-sm">{hop.position}. {hop.provider}/{hop.model}</div>
                  <div className="text-xs text-[var(--color-muted)]">{hop.local ? "local" : "cloud"}</div>
                </div>
                <CostChip billing={hop.billing} label={hop.cost_label} />
              </li>
            ))}
          </ul>
        )}
        <div className="mt-2 text-xs text-[var(--color-muted)]">
          Paid fallback: {policy.allow_paid_fallback ? "UNLOCKED" : "locked (Free Mode)"} · {chain.data?.policy}
        </div>
      </Card>

      <Card title="Models" subtitle={`${all.length} offerable now`} actions={<Input value={filter} onChange={setFilter} placeholder="Filter…" />}>
        {models.loading && !all.length ? (
          <Spinner label="Reading models from the Hermes runtime…" />
        ) : all.length === 0 ? (
          <Empty
            title="No models available"
            hint="Configure a provider (Providers page), enable the Nous free tier, or register a local endpoint."
          />
        ) : (
          <div className="space-y-3">
            {Array.from(byProvider.entries()).map(([provider, items]) => (
              <div key={provider}>
                <div className="mb-1 flex items-center gap-2">
                  <span className="text-sm font-medium">{provider}</span>
                  <CostChip billing={items[0].billing} label={items[0].cost_label} />
                  <Badge>{items.length}</Badge>
                </div>
                <ul className="space-y-1.5">
                  {items.slice(0, 40).map((item: any) => (
                    <li key={`${item.provider}/${item.id}`} className="flex items-center justify-between gap-2 rounded-xl border border-[var(--color-border)] p-2.5">
                      <button className="min-w-0 flex-1 text-left" onClick={() => setSelected(item)}>
                        <div className="truncate text-[13px]">{item.id}</div>
                        <div className="truncate text-[11px] text-[var(--color-muted)]">
                          {item.is_current ? "ACTIVE · " : ""}
                          {item.local ? "local" : "cloud"}
                          {item.capabilities?.reasoning ? " · reasoning" : ""}
                        </div>
                      </button>
                      <div className="flex shrink-0 gap-1.5">
                        <Button onClick={() => test(item)} className="!px-2 !py-1 !text-xs">Test</Button>
                        <Button variant="primary" onClick={() => select(item)} className="!px-2 !py-1 !text-xs">Use</Button>
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Sheet
        open={Boolean(selected)}
        title={selected ? `${selected.provider} / ${selected.id}` : ""}
        onClose={() => setSelected(null)}
        footer={
          <>
            <Button onClick={() => setSelected(null)}>Close</Button>
            <Button variant="primary" onClick={() => selected && select(selected)}>Set as active</Button>
          </>
        }
      >
        {selected && (
          <>
            <CostChip billing={selected.billing} label={selected.cost_label} />
            <p className="text-[13px] text-[var(--color-muted)]">{selected.notes}</p>
            <Row label="Billing class" value={selected.billing} />
            <Row label="Card required" value={selected.credit_card_required === null ? "unverified" : selected.credit_card_required ? "yes" : "no"} />
            <Row label="Runs on" value={selected.local ? "your hardware" : "provider cloud"} />
            <Row label="Verified" value={selected.as_of} />
            {selected.verify_url && (
              <a className="text-sm text-[var(--color-accent-2)] underline" href={selected.verify_url} target="_blank" rel="noreferrer">
                Check the provider's pricing
              </a>
            )}
            {!selected.free && (
              <InfoNote tone="warn">
                This is a paid model. Switching requires paid fallback to be unlocked in Providers → Free Mode.
                <label className="mt-2 flex items-center gap-2 text-xs">
                  <input type="checkbox" checked={confirmPaid} onChange={(event) => setConfirmPaid(event.target.checked)} />
                  I understand this may incur charges
                </label>
              </InfoNote>
            )}
          </>
        )}
      </Sheet>
    </>
  );
}
