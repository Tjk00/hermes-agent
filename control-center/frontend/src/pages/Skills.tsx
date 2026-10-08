import { useMemo, useState } from "react";
import { api } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { Badge, Button, Card, Empty, ErrorNote, InfoNote, Input, Sheet, Spinner, Stat } from "../components/ui";

type Skill = {
  name: string;
  description?: string;
  category?: string;
  enabled?: boolean;
  usage?: number;
  provenance?: string;
};

export default function Skills({ notify }: { notify: (message: string, tone?: "ok" | "warn" | "error" | "info") => void }) {
  const skills = useAsync<Skill[]>(() => api.get("/api/cc/skills"), [], 30000);
  const [filter, setFilter] = useState("");
  const [editing, setEditing] = useState<{ name: string; content: string; new: boolean } | null>(null);
  const [hubQuery, setHubQuery] = useState("");
  const hub = useAsync<any>(
    () => (hubQuery.trim() ? api.get("/api/cc/skills/hub/search", { q: hubQuery.trim() }) : Promise.resolve(null)),
    [hubQuery],
    0,
  );

  const list = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return (skills.data ?? []).filter(
      (skill) =>
        !needle ||
        `${skill.name} ${skill.description ?? ""} ${skill.category ?? ""}`.toLowerCase().includes(needle),
    );
  }, [skills.data, filter]);

  const categories = useMemo(() => {
    const groups = new Map<string, Skill[]>();
    for (const skill of list) {
      const key = skill.category || "uncategorised";
      groups.set(key, [...(groups.get(key) ?? []), skill]);
    }
    return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [list]);

  const enabledCount = (skills.data ?? []).filter((skill) => skill.enabled).length;

  const toggle = async (skill: Skill) => {
    try {
      await api.put("/api/cc/skills/toggle", { name: skill.name, enabled: !skill.enabled });
      notify(`${skill.name} ${skill.enabled ? "disabled" : "enabled"}.`, "ok");
      skills.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const open = async (skill: Skill) => {
    try {
      const payload = await api.get<any>("/api/cc/skills/content", { name: skill.name });
      setEditing({ name: skill.name, content: payload?.content ?? "", new: false });
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const save = async () => {
    if (!editing) return;
    try {
      if (editing.new) {
        await api.post("/api/cc/skills", { name: editing.name, content: editing.content });
      } else {
        await api.put("/api/cc/skills/content", { name: editing.name, content: editing.content });
      }
      notify(`${editing.name} saved.`, "ok");
      setEditing(null);
      skills.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  const install = async (result: any) => {
    try {
      await api.post("/api/cc/skills/hub/install", { name: result.name ?? result.slug, source: result.source });
      notify(`${result.name ?? result.slug} installed.`, "ok");
      skills.reload();
    } catch (error) {
      notify((error as Error).message, "error");
    }
  };

  return (
    <>
      <ErrorNote message={skills.error} />
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Skills" value={skills.data?.length ?? "—"} />
        <Stat label="Enabled" value={enabledCount} tone="ok" />
        <Stat label="Categories" value={categories.length} />
        <Stat label="Source" value="Hermes state" hint="this is the agent's own skill library" />
      </div>

      <Card
        title="Skills"
        subtitle="Instructions the agent loads on demand — Hermes writes and improves these itself"
        actions={
          <div className="flex gap-2">
            <Button onClick={() => setEditing({ name: "", content: "", new: true })}>New</Button>
            <Button onClick={() => skills.reload()}>Refresh</Button>
          </div>
        }
      >
        <Input value={filter} onChange={setFilter} placeholder="Filter skills…" />
        {skills.loading && !list.length ? (
          <Spinner />
        ) : list.length === 0 ? (
          <Empty title="No skills yet" hint="Skills appear here as Hermes creates them, or add your own." />
        ) : (
          <div className="mt-3 space-y-4">
            {categories.map(([category, items]) => (
              <div key={category}>
                <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-[var(--color-muted)]">
                  {category} · {items.length}
                </div>
                <ul className="space-y-2">
                  {items.map((skill) => (
                    <li key={skill.name} className="rounded-xl border border-[var(--color-border)] p-2.5">
                      <div className="flex items-start justify-between gap-2">
                        <button className="min-w-0 text-left" onClick={() => open(skill)}>
                          <div className="truncate text-sm font-medium">{skill.name}</div>
                          <div className="line-clamp-2 text-xs text-[var(--color-muted)]">{skill.description || "No description"}</div>
                        </button>
                        <Badge tone={skill.enabled ? "ok" : "neutral"}>{skill.enabled ? "ON" : "OFF"}</Badge>
                      </div>
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        <Button className="!py-1.5 !text-xs" onClick={() => toggle(skill)}>
                          {skill.enabled ? "Disable" : "Enable"}
                        </Button>
                        <Button className="!py-1.5 !text-xs" onClick={() => open(skill)}>View / edit</Button>
                        {skill.provenance && <span className="text-[11px] text-[var(--color-muted)]">{skill.provenance}</span>}
                        {typeof skill.usage === "number" && (
                          <span className="text-[11px] text-[var(--color-muted)]">used {skill.usage}×</span>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="Skill hub" subtitle="Search upstream's published skills and install one">
        <Input value={hubQuery} onChange={setHubQuery} placeholder="Search the hub… (needs the runtime running)" />
        {hub.loading ? <Spinner /> : null}
        {hub.error ? <ErrorNote message={hub.error} /> : null}
        <ul className="mt-2 space-y-2">
          {(hub.data?.results ?? hub.data?.skills ?? []).slice(0, 25).map((result: any, index: number) => (
            <li key={index} className="rounded-xl border border-[var(--color-border)] p-2.5">
              <div className="text-sm font-medium">{result.name ?? result.slug}</div>
              <div className="line-clamp-2 text-xs text-[var(--color-muted)]">{result.description ?? result.summary ?? ""}</div>
              <Button className="mt-2 !py-1.5 !text-xs" onClick={() => install(result)}>Install</Button>
            </li>
          ))}
        </ul>
        <InfoNote>Hub skills are third-party content. Read what a skill does before enabling it.</InfoNote>
      </Card>

      <Sheet
        open={Boolean(editing)}
        title={editing?.new ? "New skill" : editing?.name ?? ""}
        onClose={() => setEditing(null)}
        footer={
          <>
            <Button onClick={() => setEditing(null)}>Cancel</Button>
            <Button variant="primary" onClick={save} disabled={!editing?.name.trim() || !editing?.content.trim()}>
              Save
            </Button>
          </>
        }
      >
        {editing?.new && (
          <Input
            label="Skill name"
            value={editing.name}
            onChange={(value) => setEditing({ ...editing, name: value })}
            placeholder="my-skill"
          />
        )}
        <label className="block">
          <span className="mb-1 block text-xs text-[var(--color-muted)]">Instructions (markdown)</span>
          <textarea
            className="min-h-[45vh] w-full rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-2)] p-3 text-[13px] outline-none"
            value={editing?.content ?? ""}
            onChange={(event) => setEditing(editing ? { ...editing, content: event.target.value } : null)}
          />
        </label>
      </Sheet>
    </>
  );
}
