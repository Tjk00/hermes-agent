import { useEffect, useState } from "react";
import { api, request, setCsrfToken } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { relativeTime, shortDate } from "../lib/format";
import { Badge, Button, Card, Empty, ErrorNote, InfoNote, Input, Row, Section, Spinner, Toggle } from "../components/ui";

const TOGGLES: { key: string; label: string; hint: string; dangerous?: boolean }[] = [
  { key: "free_mode", label: "FREE MODE", hint: "Blocks paid providers from being selected, chained or scheduled." },
  { key: "nous_free_tier_enabled", label: "Nous free tier", hint: "Uses Hermes' anonymous free-tier identity (LIMITED FREE TIER — gated upstream)." },
  { key: "runtime_autostart", label: "Start runtime with the Control Center", hint: "Supervisor starts Hermes when the app boots." },
  { key: "sync_keys_to_hermes_env", label: "Mirror keys into $HERMES_HOME/.env", hint: "Plaintext copy (0600) so the Hermes CLI sees your keys. Off is safer.", dangerous: true },
  { key: "allow_paid_fallback", label: "Allow paid fallback", hint: "Only after you explicitly confirm. Paid routes never activate on their own.", dangerous: true },
];

export default function Settings({ notify, onLogout }: { notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void; onLogout: () => void }) {
  const settings = useAsync<any>(() => api.get("/api/cc/settings"), [], 0);
  const about = useAsync<any>(() => api.get("/api/cc/settings/about").catch(() => null), [], 0);
  const backups = useAsync<any>(() => api.get("/api/cc/settings/backups").catch(() => null), [], 0);
  const sessions = useAsync<any>(() => api.get("/api/cc/auth/sessions").catch(() => null), [], 0);
  const update = useAsync<any>(() => api.get("/api/cc/update/status").catch(() => null), [], 0);
  const [values, setValues] = useState<Record<string, any>>({});
  const [password, setPassword] = useState({ current: "", next: "" });
  const [busy, setBusy] = useState("");

  useEffect(() => {
    if (settings.data?.values) setValues(settings.data.values);
  }, [settings.data]);

  const save = async (patch: Record<string, any>) => {
    setBusy("save");
    try {
      const result = await api.put<any>("/api/cc/settings", { values: patch });
      notify(result?.rejected?.length ? `Saved. Rejected: ${result.rejected.join(", ")}` : "Settings saved.", "ok");
      settings.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const confirmPaid = (next: boolean) => {
    if (!next) {
      void save({ allow_paid_fallback: false });
      return;
    }
    const typed = window.prompt(
      "Paid providers can charge your own accounts. Type EXACTLY:\n\nI UNDERSTAND PAID\n\nto unlock paid fallback (you can turn it off any time).",
    );
    if (typed !== "I UNDERSTAND PAID") {
      notify("Paid fallback left disabled — the confirmation phrase did not match.", "warn");
      return;
    }
    void save({ allow_paid_fallback: true, free_mode: false });
  };

  const makeBackup = async () => {
    setBusy("backup");
    try {
      const result = await api.post<any>("/api/cc/settings/backup");
      notify(`Backup created: ${result?.name ?? "ok"}`, "ok");
      backups.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  const exportConfig = async (includeSecrets: boolean) => {
    try {
      const payload = await request<any>("/api/cc/settings/export", { params: { include_secrets: includeSecrets } });
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = includeSecrets ? "control-center-config-with-encrypted-vault.json" : `control-center-config-${Date.now()}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
      notify(includeSecrets ? "Exported with the encrypted vault rows (still unreadable without your master key)." : "Configuration exported (no secrets).", includeSecrets ? "warn" : "ok");
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const importConfig = () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "application/json";
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return;
      try {
        const parsed = JSON.parse(await file.text());
        const result = await api.post<any>("/api/cc/settings/import", parsed);
        notify(result?.message ?? "Configuration imported.", "ok");
        settings.reload();
      } catch (error) {
        notify((error as Error).message, "error");
      }
    };
    input.click();
  };

  const changePassword = async () => {
    try {
      await api.post("/api/cc/auth/password", { current_password: password.current, new_password: password.next });
      setPassword({ current: "", next: "" });
      notify("Password changed.", "ok");
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const revoke = async (id: string) => {
    try {
      await api.del(`/api/cc/auth/sessions/${id}`);
      notify("Session revoked.", "ok");
      sessions.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const logout = async () => {
    try {
      await api.post("/api/cc/auth/logout");
    } catch {
      /* the cookie is cleared regardless of the response */
    }
    setCsrfToken("");
    onLogout();
  };

  const applyUpdate = async () => {
    if (!window.confirm("Run the upstream update routine? Hermes may restart.")) return;
    setBusy("update");
    try {
      const result = await api.post<any>("/api/cc/update/apply");
      notify(result?.ok ? "Update applied." : result?.message ?? "Update finished with warnings.", result?.ok ? "ok" : "warn");
    } catch (error) {
      notify((error as Error).message, "error");
    } finally {
      setBusy("");
    }
  };

  return (
    <>
      <ErrorNote message={settings.error} />

      <Card title="Behaviour" subtitle="Stored in the Control Center database">
        {settings.loading && !settings.data ? (
          <Spinner />
        ) : (
          <div className="space-y-3">
            {TOGGLES.map((item) => (
              <Toggle
                key={item.key}
                label={item.label}
                description={item.hint}
                checked={Boolean(values[item.key])}
                onChange={(next) => {
                  if (item.key === "allow_paid_fallback") confirmPaid(next);
                  else setValues({ ...values, [item.key]: next });
                }}
              />
            ))}
            {TOGGLES.filter((item) => item.key !== "allow_paid_fallback").some((item) => Boolean(values[item.key]) !== Boolean(settings.data?.values?.[item.key])) && (
              <Button variant="primary" onClick={() => save(values)} disabled={busy === "save"}>
                {busy === "save" ? "Saving…" : "Save changes"}
              </Button>
            )}
            <Section title="Log retention">
              <Input
                type="number"
                label="Keep Control Center logs for (days)"
                value={String(values.log_retention_days ?? 30)}
                onChange={(value) => setValues({ ...values, log_retention_days: Number(value) || 0 })}
              />
              <Button className="mt-2" onClick={() => save({ log_retention_days: Number(values.log_retention_days) || 30 })}>
                Save retention
              </Button>
            </Section>
          </div>
        )}
      </Card>

      <Card title="Environment" subtitle="Read from the process environment at start">
        <pre className="max-h-72 overflow-auto text-[11px]">{JSON.stringify(settings.data?.environment ?? {}, null, 2)}</pre>
        <div className="mt-2 space-y-1">
          {Object.entries(settings.data?.notes ?? {}).map(([key, text]) => (
            <div key={key} className="text-[12px] text-[var(--color-muted)]">• {String(text)}</div>
          ))}
        </div>
      </Card>

      <Card title="Configuration transfer" subtitle="Move to another host without rebuilding">
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => exportConfig(false)}>Export config (no secrets)</Button>
          <Button onClick={() => exportConfig(true)}>Export WITH secrets</Button>
          <Button onClick={importConfig}>Import config…</Button>
          <Button variant="primary" onClick={makeBackup} disabled={busy === "backup"}>
            {busy === "backup" ? "Backing up…" : "Create backup"}
          </Button>
        </div>
        <InfoNote tone="warn">
          No readable API key is ever exported. “With secrets” adds the vault's <em>encrypted</em> rows, which are useless
          without the master key in your CC_HOME on this machine.
        </InfoNote>
        <ul className="mt-3 space-y-2">
          {(backups.data?.backups ?? []).map((backup: any) => (
            <li key={backup.name} className="flex items-center justify-between gap-2 rounded-xl border border-[var(--color-border)] p-2.5">
              <div className="min-w-0">
                <div className="mono truncate text-[12px]">{backup.name}</div>
                <div className="text-[11px] text-[var(--color-muted)]">{shortDate(backup.created_at)}</div>
              </div>
              <a className="text-[12px] text-[var(--color-accent)] underline" href={`/api/cc/settings/backups/${encodeURIComponent(backup.name)}`}>
                download
              </a>
            </li>
          ))}
          {!backups.loading && !(backups.data?.backups ?? []).length && <Empty title="No backups yet" />}
        </ul>
      </Card>

      <Card title="Security" subtitle="Your administrator account">
        <Row label="Change password" value="" />
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          <Input type="password" value={password.current} onChange={(value) => setPassword({ ...password, current: value })} placeholder="Current password" />
          <Input type="password" value={password.next} onChange={(value) => setPassword({ ...password, next: value })} placeholder="New password (8+ chars)" />
        </div>
        <Button className="mt-2" onClick={changePassword} disabled={!password.current || password.next.length < 8}>Change password</Button>

        <Section title="Active sessions" hint="Cookies issued to browsers">
          <ul className="space-y-2">
            {(sessions.data?.sessions ?? []).map((session: any) => (
              <li key={session.id} className="flex items-center justify-between gap-2 rounded-xl border border-[var(--color-border)] p-2.5">
                <div className="min-w-0">
                  <div className="truncate text-[12px]">{session.user_agent || "unknown client"}</div>
                  <div className="text-[11px] text-[var(--color-muted)]">
                    {session.ip || "local"} · last seen {relativeTime(session.last_seen_at ?? session.created_at)}
                  </div>
                </div>
                <Button className="!py-1.5 !text-xs" variant="danger" onClick={() => revoke(session.id)}>Revoke</Button>
              </li>
            ))}
          </ul>
        </Section>

        <Button variant="danger" className="mt-3" onClick={logout}>Sign out of this browser</Button>
      </Card>

      <Card title="Upstream Hermes" subtitle="Update strategy: our integration layer never blocks an upstream change">
        <Row label="Pinned" value={(update.data as any)?.pinned_ref ?? (update.data as any)?.current ?? "—"} mono />
        <Row label="Available" value={(update.data as any)?.latest ?? (update.data as any)?.available ?? "—"} mono />
        {(update.data as any)?.summary && <InfoNote>{String((update.data as any).summary)}</InfoNote>}
        <div className="mt-2 flex flex-wrap gap-2">
          <Button onClick={() => update.reload()}>Check again</Button>
          <Button variant="primary" onClick={applyUpdate} disabled={busy === "update"}>Apply upstream update</Button>
        </div>
        <InfoNote>
          The Control Center talks to Hermes over its documented HTTP API and only supervises the process. Updating your
          checkout therefore does not require changes here — if upstream changes a route, the affected card says so
          instead of failing silently.
        </InfoNote>
      </Card>

      <Card title="About & attribution" subtitle="Licences and provenance">
        <Row label="Control Center" value={`${about.data?.control_center?.name ?? "Hermes Agent Control Center"} v${about.data?.control_center?.version ?? "1.0.0"}`} />
        <Row label="Licence" value={about.data?.control_center?.license ?? "MIT"} />
        <Row label="Upstream" value={`${about.data?.upstream?.project ?? "Hermes Agent"} — ${about.data?.upstream?.author ?? "Nous Research"} (${about.data?.upstream?.license ?? "MIT"})`} />
        <a className="mt-1 inline-block text-[12px] text-[var(--color-accent)] underline" href={about.data?.upstream?.source ?? "https://github.com/NousResearch/hermes-agent"} target="_blank" rel="noreferrer noopener">
          {about.data?.upstream?.source ?? "https://github.com/NousResearch/hermes-agent"}
        </a>
        <div className="mt-2 space-y-1">
          {(about.data?.notices ?? []).map((notice: string) => (
            <div key={notice} className="text-[12px] text-[var(--color-muted)]">• {notice}</div>
          ))}
        </div>
        <Badge tone="info">integration: process supervision + documented HTTP API</Badge>
      </Card>
    </>
  );
}
