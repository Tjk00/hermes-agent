import { useAsync } from "../lib/hooks";
import { api } from "../lib/api";
import { Badge, Button, Card, Empty, ErrorNote, InfoNote, Row, Spinner, Toggle } from "../components/ui";

export default function Tools({ notify }: { notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void }) {
  const toolsets = useAsync<any[]>(() => api.get("/api/cc/tools/toolsets"), [], 30000);
  const backends = useAsync<any>(() => api.get("/api/cc/tools/terminal/backends"), [], 60000);
  const computerUse = useAsync<any>(() => api.get("/api/cc/tools/computer-use/status").catch(() => null), [], 60000);

  const list = toolsets.data ?? [];

  const toggle = async (toolset: any, next: boolean) => {
    try {
      await api.put(`/api/cc/tools/toolsets/${toolset.name}`, { enabled: next });
      notify(`${toolset.name} ${next ? "enabled" : "disabled"}.`, "ok");
      toolsets.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  return (
    <>
      <ErrorNote message={toolsets.error} />
      <InfoNote>
        Toolsets are Hermes' capability bundles. Enabling one exposes its tools to the agent; tools that need an external
        service say so. Nothing here is simulated — the list comes from the running runtime.
      </InfoNote>

      <Card title="Toolsets" subtitle={`${list.filter((item) => item.enabled).length} of ${list.length} enabled`} actions={<Button onClick={() => toolsets.reload()}>Refresh</Button>}>
        {toolsets.loading && !list.length ? (
          <Spinner />
        ) : list.length === 0 ? (
          <Empty title="No toolsets reported" hint="Start the runtime, then refresh." />
        ) : (
          <ul className="space-y-2">
            {list.map((toolset: any) => (
              <li key={toolset.name} className="rounded-xl border border-[var(--color-border)] p-2.5">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium">{toolset.label ?? toolset.name}</span>
                      {toolset.available === false && <Badge tone="warn">UNAVAILABLE</Badge>}
                      {toolset.configured === false && <Badge tone="neutral">NEEDS SETUP</Badge>}
                    </div>
                    <div className="line-clamp-2 text-xs text-[var(--color-muted)]">{toolset.description}</div>
                    {toolset.tools?.length ? (
                      <div className="mt-1 flex flex-wrap gap-1">
                        {toolset.tools.slice(0, 8).map((tool: string) => (
                          <span key={tool} className="mono rounded bg-white/5 px-1.5 py-0.5 text-[10px]">{tool}</span>
                        ))}
                        {toolset.tools.length > 8 && <span className="text-[10px] text-[var(--color-muted)]">+{toolset.tools.length - 8}</span>}
                      </div>
                    ) : null}
                  </div>
                  <div className="w-14 shrink-0">
                    <Toggle label="" checked={Boolean(toolset.enabled)} onChange={(next) => toggle(toolset, next)} />
                  </div>
                </div>
                {toolset.platform_label && <div className="mt-1 text-[11px] text-[var(--color-muted)]">{toolset.platform_label}</div>}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Terminal backend" subtitle="Where Hermes runs shell commands">
        {backends.loading ? (
          <Spinner />
        ) : (
          <>
            <Row label="Active backend" value={(backends.data as any)?.active ?? (backends.data as any)?.current ?? "—"} mono />
            <ul className="mt-2 space-y-1">
              {((backends.data as any)?.backends ?? []).map((backend: any) => (
                <li key={backend.id ?? backend.name} className="flex items-center justify-between gap-2 py-1 text-[13px]">
                  <span className="truncate">{backend.label ?? backend.name ?? backend.id}</span>
                  <Badge tone={backend.available === false ? "neutral" : "ok"}>{backend.available === false ? "unavailable" : "available"}</Badge>
                </li>
              ))}
            </ul>
            <InfoNote tone="warn">
              Terminal access is powerful. Keep approvals enabled in Hermes, and remember that anything executed runs with
              your user's permissions on this host.
            </InfoNote>
          </>
        )}
      </Card>

      {computerUse.data && (
        <Card title="Computer use" subtitle="Screen control permissions">
          <Row label="Status" value={(computerUse.data as any).status ?? (computerUse.data as any).enabled ?? "—"} />
          {(computerUse.data as any).permissions && (
            <pre className="mt-2">{JSON.stringify((computerUse.data as any).permissions, null, 2)}</pre>
          )}
        </Card>
      )}
    </>
  );
}
