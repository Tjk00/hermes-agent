import { useEffect, useMemo, useRef, useState } from "react";
import { api, streamSse } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { relativeTime, truncate } from "../lib/format";
import { Badge, Button, Card, CostChip, Empty, ErrorNote, InfoNote, Input, Row, Sheet, Spinner } from "../components/ui";

type Thread = {
  id: string;
  title: string;
  model?: string;
  provider?: string;
  updated_at: string;
  message_count?: number;
  last_message?: string;
};

type Message = { id: number; role: string; content: string; created_at: string; meta?: string };

/** Minimal, dependency-free markdown for chat bubbles (code, bold, links, lists). */
function Markdown({ text }: { text: string }) {
  const blocks = useMemo(() => {
    const parts: { type: "text" | "code"; content: string; lang?: string }[] = [];
    const fence = /```(\w+)?\n?([\s\S]*?)```/g;
    let last = 0;
    let match: RegExpExecArray | null;
    while ((match = fence.exec(text)) !== null) {
      if (match.index > last) parts.push({ type: "text", content: text.slice(last, match.index) });
      parts.push({ type: "code", content: match[2], lang: match[1] });
      last = match.index + match[0].length;
    }
    if (last < text.length) parts.push({ type: "text", content: text.slice(last) });
    return parts;
  }, [text]);

  return (
    <div className="markdown text-[15px] leading-relaxed">
      {blocks.map((block, index) =>
        block.type === "code" ? (
          <div key={index} className="relative my-2">
            <pre>
              <code>{block.content}</code>
            </pre>
            <button
              className="absolute right-2 top-2 rounded-lg border border-white/10 bg-black/50 px-2 py-1 text-[11px]"
              onClick={() => void navigator.clipboard?.writeText(block.content)}
            >
              Copy
            </button>
          </div>
        ) : (
          <TextBlock key={index} content={block.content} />
        ),
      )}
    </div>
  );
}

function TextBlock({ content }: { content: string }) {
  const lines = content.split("\n");
  return (
    <>
      {lines.map((line, index) => {
        if (!line.trim()) return <div key={index} className="h-2" />;
        const bullet = /^\s*[-*]\s+/.test(line);
        const numbered = /^\s*\d+\.\s+/.test(line);
        const html = line
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/`([^`]+)`/g, '<code class="rounded bg-white/10 px-1 py-0.5 text-[13px]">$1</code>')
          .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
          .replace(
            /(https?:\/\/[^\s)]+)/g,
            '<a href="$1" target="_blank" rel="noreferrer noopener">$1</a>',
          );
        const prefix = bullet ? "• " : numbered ? "" : "";
        return (
          <p
            key={index}
            className={bullet || numbered ? "ml-3" : ""}
            dangerouslySetInnerHTML={{ __html: prefix + html }}
          />
        );
      })}
    </>
  );
}

export default function Chat({ notify }: { notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void }) {
  const threads = useAsync<{ threads: Thread[] }>(() => api.get("/api/cc/chat/threads"), [], 20000);
  const bridge = useAsync<any>(() => api.get("/api/cc/chat/bridge"), [], 20000);
  const models = useAsync<any>(() => api.get("/api/cc/models"), [], 60000);

  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState("");
  const [reasoning, setReasoning] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showThreads, setShowThreads] = useState(false);
  const [model, setModel] = useState("");
  const controller = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement | null>(null);

  const list = threads.data?.threads ?? [];
  const active = list.find((thread) => thread.id === activeId) ?? null;
  const freeModels = (models.data?.models ?? []).filter((item: any) => item.free);
  const paidUnlocked = Boolean(models.data?.policy?.allow_paid_fallback);

  useEffect(() => {
    if (!activeId && list.length) setActiveId(list[0].id);
  }, [list, activeId]);

  useEffect(() => {
    if (!activeId) {
      setMessages([]);
      return;
    }
    let cancelled = false;
    api
      .get<{ messages: Message[] }>(`/api/cc/chat/threads/${activeId}/messages`)
      .then((payload) => {
        if (!cancelled) setMessages(payload.messages);
      })
      .catch((err) => setError((err as Error).message));
    return () => {
      cancelled = true;
    };
  }, [activeId, threads.data]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, streaming]);

  const newThread = async () => {
    try {
      const created = await api.post<{ thread: Thread }>("/api/cc/chat/threads", { title: "New conversation" });
      threads.reload();
      setActiveId(created.thread.id);
      setMessages([]);
      setShowThreads(false);
    } catch (err) {
      notify((err as Error).message, "error");
    }
  };

  const renameThread = async (thread: Thread) => {
    const title = window.prompt("Rename conversation", thread.title);
    if (!title) return;
    try {
      await api.patch(`/api/cc/chat/threads/${thread.id}`, { title });
      threads.reload();
    } catch (err) {
      notify((err as Error).message, "error");
    }
  };

  const deleteThread = async (thread: Thread) => {
    if (!window.confirm(`Delete "${thread.title}"? The transcript is removed from the Control Center.`)) return;
    try {
      await api.del(`/api/cc/chat/threads/${thread.id}`);
      if (activeId === thread.id) {
        setActiveId(null);
        setMessages([]);
      }
      threads.reload();
      notify("Conversation deleted.", "ok");
    } catch (err) {
      notify((err as Error).message, "error");
    }
  };

  const send = async (overrideText?: string) => {
    const text = (overrideText ?? draft).trim();
    if (!text || sending) return;
    let threadId = activeId;
    if (!threadId) {
      const created = await api.post<{ thread: Thread }>("/api/cc/chat/threads", { title: text.slice(0, 60) });
      threadId = created.thread.id;
      setActiveId(threadId);
      threads.reload();
    }
    setError(null);
    setDraft("");
    setSending(true);
    setStreaming("");
    setReasoning("");
    const optimistic: Message = { id: Date.now(), role: "user", content: text, created_at: new Date().toISOString() };
    setMessages((current) => [...current, optimistic]);
    controller.current = new AbortController();
    let assistantText = "";
    try {
      await streamSse(`/api/cc/chat/threads/${threadId}/messages`, { content: text, model }, {
        signal: controller.current.signal,
        onEvent: (event, data) => {
          if (event === "delta") {
            assistantText += data.text ?? "";
            setStreaming(assistantText);
          } else if (event === "reasoning") {
            setReasoning((current) => current + (data.text ?? ""));
          } else if (event === "error") {
            setError(data.message ?? "The agent returned an error.");
          } else if (event === "done") {
            setMessages((current) => [
              ...current,
              {
                id: data.message_id ?? Date.now(),
                role: "assistant",
                content: data.content ?? assistantText,
                created_at: new Date().toISOString(),
                meta: JSON.stringify(data.meta ?? {}),
              },
            ]);
            setStreaming("");
            setReasoning("");
          }
        },
        onError: (err) => setError(err.message),
      });
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSending(false);
      controller.current = null;
      threads.reload();
    }
  };

  const stop = async () => {
    controller.current?.abort();
    if (activeId) {
      try {
        await api.post(`/api/cc/chat/threads/${activeId}/stop`);
      } catch {
        /* stopping is best-effort */
      }
    }
    setSending(false);
    notify("Generation stopped.", "info");
  };

  const retry = async () => {
    if (!activeId) return;
    setError(null);
    setSending(true);
    setStreaming("");
    let assistantText = "";
    await streamSse(`/api/cc/chat/threads/${activeId}/retry`, {}, {
      onEvent: (event, data) => {
        if (event === "delta") {
          assistantText += data.text ?? "";
          setStreaming(assistantText);
        } else if (event === "error") {
          setError(data.message ?? "Retry failed.");
        } else if (event === "done") {
          setMessages((current) => [
            ...current,
            { id: data.message_id ?? Date.now(), role: "assistant", content: data.content ?? assistantText, created_at: new Date().toISOString() },
          ]);
          setStreaming("");
        }
      },
    });
    setSending(false);
    const payload = await api.get<{ messages: Message[] }>(`/api/cc/chat/threads/${activeId}/messages`);
    setMessages(payload.messages);
  };

  const enableBridge = async () => {
    try {
      notify("Enabling the agent bridge…", "info");
      const result = await api.post<any>("/api/cc/chat/bridge/enable", {});
      if (result.restart_required) {
        notify(result.next ?? "Restart the runtime to apply.", "warn");
        await api.post("/api/cc/runtime/restart");
        await new Promise((resolve) => setTimeout(resolve, 6000));
      }
      const started = await api.post<any>("/api/cc/chat/bridge/start");
      notify(started.ok ? "Agent bridge is live." : started.message, started.ok ? "ok" : "error");
      bridge.reload();
    } catch (err) {
      notify((err as Error).message, "error");
    }
  };

  const testInference = async () => {
    try {
      notify("Sending a real prompt…", "info");
      const result = await api.post<any>("/api/cc/chat/test", {});
      notify(result.ok ? `Provider answered in ${result.latency_ms}ms: ${result.reply?.slice(0, 80)}` : result.message, result.ok ? "ok" : "error");
    } catch (err) {
      notify((err as Error).message, "error");
    }
  };

  const bridgeReady = Boolean(bridge.data?.health?.reachable);

  return (
    <>
      <Card
        title="Chat with Hermes"
        subtitle={
          bridgeReady
            ? `Bridge live · ${bridge.data?.config?.base_url}`
            : "The agent bridge is not running yet — enable it to talk to the real agent."
        }
        actions={
          <div className="flex gap-2">
            <Button onClick={() => setShowThreads(true)}>Conversations</Button>
            <Button variant="primary" onClick={newThread}>
              New
            </Button>
          </div>
        }
      >
        {!bridgeReady && (
          <InfoNote tone="warn">
            <p className="mb-2">
              Chat uses Hermes' own OpenAI-compatible server, which runs inside the agent gateway. It is the same agent
              core as the CLI — your tools, memory and skills. Nothing is simulated here: if the bridge is off, no reply
              is invented.
            </p>
            <div className="flex flex-wrap gap-2">
              <Button variant="primary" onClick={enableBridge}>
                Enable & start agent bridge
              </Button>
              <Button onClick={testInference}>Test inference</Button>
            </div>
            {bridge.data?.health?.reason && (
              <p className="mt-2 text-xs opacity-80">Status: {bridge.data.health.reason}</p>
            )}
          </InfoNote>
        )}
        {bridgeReady && (
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone="ok">BRIDGE READY</Badge>
            <CostChip
              billing={models.data?.current?.billing}
              label={models.data?.current?.cost_label || (freeModels.length ? "FREE-FIRST" : "UNVERIFIED")}
            />
            {active && <span className="text-[11px] text-[var(--color-muted)]">in “{truncate(active.title, 40)}”</span>}
            <Button onClick={testInference}>Test inference</Button>
            <Button
              variant="danger"
              onClick={async () => {
                await api.post("/api/cc/chat/bridge/stop");
                bridge.reload();
              }}
            >
              Stop bridge
            </Button>
          </div>
        )}
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          <Input
            label="Model for this conversation (empty = runtime default)"
            value={model}
            onChange={setModel}
            placeholder={models.data?.current?.model || "hermes-agent"}
            hint={
              freeModels.length
                ? `${freeModels.length} free-capable model(s) available${paidUnlocked ? " · paid unlocked" : " · paid locked"}`
                : "No free models listed yet — configure a provider or enable the free tier."
            }
          />
          <div className="text-xs text-[var(--color-muted)]">
            <div>Provider: {models.data?.current?.provider || bridge.data?.gateway?.gateway_platforms?.api_server ? "nous" : "—"}</div>
            <div>Cost mode: {models.data?.current?.cost_label || (freeModels.length ? "free-first" : "undetermined")}</div>
            <div>Agent status: {bridge.data?.gateway?.gateway_running ? "gateway running" : "gateway stopped"}</div>
          </div>
        </div>
      </Card>

      <ErrorNote message={error} />

      <Card className="flex min-h-[45vh] flex-col">
        <div className="flex-1 space-y-3">
          {messages.length === 0 && !streaming && (
            <Empty
              title="No messages yet"
              hint={
                bridgeReady
                  ? "Ask Hermes anything. Tool calls, memory and skills behave exactly as they do in the CLI."
                  : "Enable the agent bridge above to start a real conversation."
              }
            />
          )}
          {messages.map((message) => (
            <div key={message.id} className={message.role === "user" ? "flex justify-end" : ""}>
              <div
                className={`max-w-[92%] rounded-2xl px-3 py-2 ${
                  message.role === "user"
                    ? "bg-[var(--color-accent)]/15 border border-[var(--color-accent)]/30"
                    : "bg-[var(--color-surface-2)] border border-[var(--color-border)]"
                }`}
              >
                <div className="mb-0.5 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
                  {message.role === "user" ? "You" : "Hermes"} · {relativeTime(message.created_at)}
                </div>
                <Markdown text={message.content} />
                {message.role === "assistant" && (
                  <div className="mt-2 flex gap-3">
                    <button
                      className="text-[11px] text-[var(--color-muted)]"
                      onClick={() => void navigator.clipboard?.writeText(message.content)}
                    >
                      Copy
                    </button>
                  </div>
                )}
              </div>
            </div>
          ))}
          {streaming && (
            <div className="max-w-[92%] rounded-2xl border border-[var(--color-border)] bg-[var(--color-surface-2)] px-3 py-2">
              <div className="mb-0.5 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">Hermes · streaming</div>
              <Markdown text={streaming} />
              <span className="pulse">▍</span>
            </div>
          )}
          {reasoning && !streaming && (
            <details className="text-xs text-[var(--color-muted)]">
              <summary>Reasoning trace</summary>
              <pre>{reasoning}</pre>
            </details>
          )}
          <div ref={bottom} />
        </div>

        <div className="mt-3 flex items-end gap-2">
          <textarea
            className="min-h-[44px] w-full flex-1 resize-y rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-2)] px-3 py-2.5 text-[15px] outline-none focus:border-[var(--color-accent-2)]"
            placeholder={bridgeReady ? "Message Hermes…" : "Enable the bridge to chat"}
            rows={1}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void send();
            }}
          />
          {sending ? (
            <Button variant="danger" onClick={stop}>
              Stop
            </Button>
          ) : (
            <Button variant="primary" onClick={() => void send()} disabled={!draft.trim()}>
              Send
            </Button>
          )}
        </div>
        <div className="mt-2 flex items-center justify-between text-[11px] text-[var(--color-muted)]">
          <span>⌘/Ctrl + Enter to send</span>
          {messages.some((message) => message.role === "assistant") && (
            <button className="underline" onClick={retry}>
              Retry last
            </button>
          )}
        </div>
      </Card>

      <Sheet open={showThreads} title="Conversations" onClose={() => setShowThreads(false)}>
        <Button variant="primary" onClick={newThread}>
          + New conversation
        </Button>
        {threads.loading && !list.length ? (
          <Spinner />
        ) : list.length === 0 ? (
          <Empty title="No conversations yet" />
        ) : (
          <ul className="space-y-2">
            {list.map((thread) => (
              <li
                key={thread.id}
                className={`rounded-xl border p-3 ${
                  thread.id === activeId ? "border-[var(--color-accent)]/40 bg-white/5" : "border-[var(--color-border)]"
                }`}
              >
                <button
                  className="w-full text-left"
                  onClick={() => {
                    setActiveId(thread.id);
                    setShowThreads(false);
                  }}
                >
                  <div className="truncate text-sm font-medium">{thread.title}</div>
                  <div className="truncate text-xs text-[var(--color-muted)]">
                    {thread.message_count ?? 0} messages · {relativeTime(thread.updated_at)}
                  </div>
                </button>
                <div className="mt-2 flex gap-2">
                  <Button onClick={() => renameThread(thread)} className="!py-1.5 !text-xs">
                    Rename
                  </Button>
                  <Button variant="danger" onClick={() => deleteThread(thread)} className="!py-1.5 !text-xs">
                    Delete
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Sheet>

      <Card title="Bridge details" subtitle="What the Control Center talks to">
        <Row label="Configured" value={bridge.data?.config?.configured ? "yes" : "no"} />
        <Row label="Address" value={bridge.data?.config?.base_url ?? "—"} mono />
        <Row label="Key stored" value={bridge.data?.config?.key_stored ? "yes (encrypted)" : "no"} />
        <Row label="Reachable" value={bridge.data?.health?.reachable ? "yes" : "no"} />
        <Row label="Gateway" value={bridge.data?.gateway?.gateway_state ?? "stopped"} />
        <p className="mt-2 text-xs text-[var(--color-muted)]">{bridge.data?.explain}</p>
      </Card>
    </>
  );
}
