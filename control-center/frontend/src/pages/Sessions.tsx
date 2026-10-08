import { useState } from "react";
import { api } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { relativeTime, truncate } from "../lib/format";
import { Badge, Button, Card, Empty, ErrorNote, InfoNote, Input, Row, Sheet, Spinner, Stat } from "../components/ui";

function SessionDetail({ id, onClose, onDeleted, notify }: {
  id: string | null;
  onClose: () => void;
  onDeleted: () => void;
  notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void;
}) {
  const detail = useAsync<any>(() => (id ? api.get(`/api/cc/sessions/${id}`) : Promise.resolve(null)), [id]);
  const messages = useAsync<any>(
    () => (id ? api.get(`/api/cc/sessions/${id}/messages`, { limit: 200 }) : Promise.resolve(null)),
    [id],
  );
  const session = detail.data?.session ?? detail.data;
  const list = messages.data?.messages ?? messages.data ?? [];

  const remove = async () => {
    if (!id || !window.confirm("Delete this session from Hermes' history?")) return;
    try {
      await api.del(`/api/cc/sessions/${id}`);
      notify("Session deleted.", "ok");
      onDeleted();
      onClose();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  return (
    <Sheet open={Boolean(id)} title={session?.title ?? "Session"} onClose={onClose} footer={<>
      <Button variant="danger" onClick={remove}>Delete session</Button>
    </>}>
      {detail.loading ? <Spinner /> : null}
      {session && (
        <div className="mb-3">
          <Row label="Session id" value={id ?? ""} mono />
          <Row label="Messages" value={String(session.message_count ?? list.length ?? "—")} />
          <Row label="Started" value={relativeTime(session.created_at ?? session.started_at)} />
          <Row label="Last activity" value={relativeTime(session.updated_at ?? session.ended_at)} />
          {session.model && <Row label="Model" value={`${session.provider ? session.provider + "/" : ""}${session.model}`} mono />}
          {session.source && <Row label="Source" value={session.source} />}
        </div>
      )}
      <div className="space-y-2">
        {list.map((message: any, index: number) => (
          <div key={index} className="rounded-xl border border-[var(--color-border)] p-2.5">
            <div className="mb-1 flex items-center gap-2 text-[11px] text-[var(--color-muted)]">
              <Badge tone={message.role === "user" ? "neutral" : "info"}>{message.role ?? "?"}</Badge>
              <span>{relativeTime(message.ts ?? message.created_at)}</span>
              {message.model && <span className="mono truncate">{message.model}</span>}
            </div>
            <div className="whitespace-pre-wrap break-words text-[13px]">{truncate(String(message.content ?? ""), 1200)}</div>
          </div>
        ))}
        {!messages.loading && !list.length && <Empty title="No messages stored for this session" />}
      </div>
    </Sheet>
  );
}

export default function Sessions({ notify }: { notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void }) {
  const [q, setQ] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);
  const sessions = useAsync<any>(() => api.get("/api/cc/sessions", q ? { q } : {}), [q], 30000);
  const stats = useAsync<any>(() => api.get("/api/cc/sessions/stats").catch(() => null), [], 60000);
  const list = sessions.data?.sessions ?? sessions.data ?? [];

  return (
    <>
      <ErrorNote message={sessions.error} />
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Sessions" value={stats.data?.total ?? stats.data?.sessions ?? list.length} />
        <Stat label="Messages" value={stats.data?.messages ?? "—"} />
        <Stat label="Tokens" value={stats.data?.tokens ?? "—"} />
        <Stat label="Storage" value={stats.data?.size ?? "—"} hint="Hermes state database" />
      </div>

      <Card
        title="Conversation history"
        subtitle="Recorded by Hermes itself — this is its own session store, not a copy kept by the Control Center"
        actions={<Button onClick={() => sessions.reload()}>Refresh</Button>}
      >
        <Input value={q} onChange={setQ} placeholder="Search sessions…" />
        {sessions.loading && !list.length ? (
          <Spinner />
        ) : list.length === 0 ? (
          <Empty title="No sessions yet" hint="Start a chat and it will appear here." />
        ) : (
          <ul className="mt-3 space-y-2">
            {list.map((session: any) => (
              <li key={session.id}>
                <button
                  className="w-full rounded-xl border border-[var(--color-border)] p-2.5 text-left hover:border-[var(--color-accent)]"
                  onClick={() => setOpenId(session.id)}
                >
                  <div className="flex items-start justify-between gap-2">
                    <span className="truncate text-sm font-medium">{session.title ?? session.id}</span>
                    <span className="shrink-0 text-[11px] text-[var(--color-muted)]">
                      {relativeTime(session.updated_at ?? session.created_at)}
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-[var(--color-muted)]">
                    <span>{session.message_count ?? 0} messages</span>
                    {session.model && <span className="mono truncate">{session.model}</span>}
                    {session.source && <span>{session.source}</span>}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <InfoNote>
        Sessions live in Hermes' own database (SQLite by default). Deleting one here deletes it there — there is no
        hidden second copy.
      </InfoNote>

      <SessionDetail id={openId} onClose={() => setOpenId(null)} onDeleted={() => sessions.reload()} notify={notify} />
    </>
  );
}
