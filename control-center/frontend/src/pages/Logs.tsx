import { useState } from "react";
import { api, request } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { shortDate, truncate } from "../lib/format";
import { Badge, Button, Card, Empty, ErrorNote, InfoNote, Input, Card as Panel, Spinner } from "../components/ui";

const LEVELS = ["", "INFO", "WARNING", "ERROR"];
const SOURCES = [
  { id: "control-center", label: "Control Center" },
  { id: "runtime", label: "Runtime process" },
  { id: "hermes", label: "Hermes agent" },
  { id: "all", label: "Everything" },
];

const CATEGORIES = ["", "INFO", "WARNING", "ERROR", "MODEL", "PROVIDER", "SECURITY", "SCHEDULER", "SYSTEM"];

function save(name: string, text: string) {
  const blob = new Blob([text], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export default function Logs({ notify }: { notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void }) {
  const [level, setLevel] = useState("");
  const [category, setCategory] = useState("");
  const [q, setQ] = useState("");
  const [source, setSource] = useState("control-center");

  const categories = useAsync<any>(() => api.get("/api/cc/logs/categories").catch(() => null), [], 0);
  const logs = useAsync<any>(() => api.get("/api/cc/logs", { level, category, q, limit: 500, source }), [level, category, q, source], 8000);

  const data = logs.data ?? {};
  const entries: any[] =
    source === "all" ? data.merged ?? [] : data.sources?.[source]?.entries ?? [];
  const sourceError: string = data.sources?.[source]?.error ?? "";
  const sourceNote: string = data.sources?.[source]?.note ?? "";
  const logFile: string = data.sources?.runtime?.file ?? "";

  const exportLogs = async (format: string) => {
    try {
      const text = await request<string>("/api/cc/logs/export", { params: { format, limit: 5000 } });
      save(`control-center-logs-${Date.now()}.${format}`, text);
      notify("Export downloaded — secrets are already redacted.", "ok");
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const clear = async () => {
    if (source === "hermes") {
      notify("Hermes' own log files are managed by Hermes — use the Hermes CLI.", "warn");
      return;
    }
    if (!window.confirm(`Clear ${source} logs?`)) return;
    try {
      const result = await api.del<any>("/api/cc/logs", { source: source === "all" ? "control-center" : source });
      notify(result?.removed ? `${result.removed} entries cleared.` : "Cleared.", "ok");
      logs.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  return (
    <>
      <ErrorNote message={logs.error} />

      <Card title="Logs" subtitle="Categorised so you can answer “what broke?” quickly" actions={<Button onClick={() => logs.reload()}>Refresh</Button>}>
        <div className="flex flex-wrap gap-2">
          {SOURCES.map((option) => (
            <Button key={option.id} variant={source === option.id ? "primary" : "default"} onClick={() => setSource(option.id)} className="!py-1.5 !text-xs">
              {option.label}
            </Button>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          {LEVELS.map((option) => (
            <Button key={option || "all"} variant={level === option ? "primary" : "default"} onClick={() => setLevel(option)} className="!py-1.5 !text-xs">
              {option || "All levels"}
            </Button>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          {((categories.data?.categories as string[]) ?? CATEGORIES).map((item) => (
            <Button key={item || "any"} variant={category === item ? "primary" : "default"} onClick={() => setCategory(item)} className="!py-1.5 !text-xs">
              {item || "Any category"}
            </Button>
          ))}
        </div>
        <div className="mt-3">
          <Input value={q} onChange={setQ} placeholder="Search text…" />
        </div>
      </Card>

      {sourceError && (
        <ErrorNote
          message={`Hermes' own log could not be read: ${sourceError}`}
          action="Start the runtime (Dashboard page) or check that the session token is valid."
        />
      )}
      {sourceNote && <InfoNote>{sourceNote}</InfoNote>}

      <Panel title={`${entries.length} entries`} subtitle={source === "runtime" && logFile ? logFile : undefined}>
        {logs.loading && !entries.length ? (
          <Spinner />
        ) : entries.length === 0 ? (
          <Empty title="Nothing here yet" hint="Level filters and search apply to the Control Center store; runtime lines are filtered by text." />
        ) : (
          <ul className="divide-y divide-white/5">
            {entries.map((entry: any, index: number) => (
              <li key={index} className="py-2">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <Badge tone={entry.level === "ERROR" ? "error" : entry.level === "WARNING" ? "warn" : entry.category === "SECURITY" ? "info" : "neutral"}>
                      {entry.level ?? "INFO"}
                    </Badge>
                    {entry.category && <span className="text-[11px] text-[var(--color-muted)]">{entry.category}</span>}
                    {entry.source && <span className="text-[11px] text-[var(--color-muted)]">· {entry.source}</span>}
                  </div>
                  <span className="shrink-0 text-[11px] text-[var(--color-muted)]">{entry.ts ? shortDate(entry.ts) : ""}</span>
                </div>
                <div className="mono mt-1 whitespace-pre-wrap break-words text-[12px]">{truncate(String(entry.message ?? ""), 900)}</div>
              </li>
            ))}
          </ul>
        )}
        <div className="mt-3 flex flex-wrap gap-2">
          <Button onClick={() => exportLogs("csv")}>Export CSV</Button>
          <Button onClick={() => exportLogs("json")}>Export JSON</Button>
          <Button onClick={() => exportLogs("txt")}>Export text</Button>
          <Button variant="danger" onClick={clear} disabled={source === "hermes"}>Clear</Button>
        </div>
      </Panel>

      <InfoNote>
        API keys and tokens are redacted by a logging filter before they reach the store, the export, or this page. Log
        files are owner-only (0600). Nothing leaves your server.
      </InfoNote>
    </>
  );
}
