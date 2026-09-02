import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Boxes,
  Check,
  Download,
  Link2,
  Package,
  RefreshCw,
  Trash2,
  Unlink,
  Upload,
} from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { noteGitopsUnlinked } from "@/features/settings/GitopsPanel";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useSyncStatus } from "@/lib/queries";
import type { SyncProjectState, SyncProjectStatus } from "@/lib/types";
import { useSearchParams } from "react-router-dom";

// A self-hosted instance links to a cloud tenant, then pushes its locally-built code graph up
// so cloud-side triage/collision-clustering can reason across the whole repo (AL-141). The
// link credential is stored server-side (encrypted at rest) and never returned here.

const STATUS_META: Record<SyncProjectStatus, { label: string; cls: string; dot: string }> = {
  live: { label: "IN SYNC", cls: "text-st-done", dot: "bg-st-done" },
  stale: { label: "STALE", cls: "text-st-review", dot: "bg-st-review" },
  paused: { label: "PAUSED", cls: "text-muted", dot: "bg-faint" },
  unsynced: { label: "NOT SYNCED", cls: "text-faint", dot: "bg-faint-2" },
  empty: { label: "EMPTY", cls: "text-faint-2", dot: "bg-faint-2" },
};

function ago(iso: string | null): string {
  if (!iso) return "never";
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 45) return "just now";
  const mins = secs / 60;
  if (mins < 60) return `${Math.round(mins)}m ago`;
  const hrs = mins / 60;
  if (hrs < 24) return `${Math.round(hrs)}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

export function SyncLinkPanel() {
  const { data: status, isLoading } = useSyncStatus();
  const [params] = useSearchParams();
  const reason = params.get("reason");
  const qc = useQueryClient();
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["sync-status"] });
    qc.invalidateQueries({ queryKey: ["platform"] });
  };
  const [scopedId, setScopedId] = React.useState<string | null>(null);

  const scoped = status?.projects.find((p) => p.project_id === scopedId) ?? null;

  if (isLoading || !status) return <p className="text-[12.5px] text-faint">Loading sync status…</p>;

  return (
    <div className="max-w-2xl space-y-6">
      <div>
        <h2 className="text-[15px] font-semibold tracking-tight">Sync / Link</h2>
        <p className="mt-1 max-w-[60ch] text-[12.5px] leading-relaxed text-muted">
          Connect this self-hosted instance to a cloud org. Mint the link key there — Settings
          → Sync / Link, or API keys → Link key — then paste the cloud URL and key
          below. This box builds the graph; the cloud holds items, claims, and memory.
          Vectors never leave the box; the cloud re-embeds.
        </p>
        {reason === "missing" && (
          <p className="mt-3 rounded-[11px] border border-st-review/30 bg-st-review/[0.06] px-3 py-2 text-[12.5px] text-st-review">
            No cloud URL is linked. Connect a tenant here rather than opening a blank address.
          </p>
        )}
        {reason === "malformed" && (
          <p className="mt-3 rounded-[11px] border border-st-review/30 bg-st-review/[0.06] px-3 py-2 text-[12.5px] text-st-review">
            The stored cloud URL is not a usable http(s) address, so it was not opened.
          </p>
        )}
      </div>

      <CloudLinkCard status={status} onChange={invalidate} scopedId={scopedId} onScope={setScopedId} />

      <ScopeBar scoped={scoped} onClear={() => setScopedId(null)} />

      <GraphPrivacyCard scoped={scoped} linked={status.linked} onChange={invalidate} />
      <GraphPushCard scoped={scoped} linked={status.linked} onChange={invalidate} />
      <PortableBundleCard scoped={scoped} onChange={invalidate} />
    </div>
  );
}

// ── 1. Cloud link ──────────────────────────────────────────────────────────────────────────

function CloudLinkCard({
  status,
  onChange,
  scopedId,
  onScope,
}: {
  status: NonNullable<ReturnType<typeof useSyncStatus>["data"]>;
  onChange: () => void;
  scopedId: string | null;
  onScope: (id: string) => void;
}) {
  const qc = useQueryClient();
  const [cloudUrl, setCloudUrl] = React.useState("");
  const [apiKey, setApiKey] = React.useState("");
  const [org, setOrg] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);

  function dropGitopsCache() {
    // Prefix of keys.gitops(id). Link is instance-wide; do not first-paint a
    // pre-link (or pre-unlink) GitopsView. Graph-push onChange must not do this.
    qc.removeQueries({ queryKey: ["gitops"] });
  }

  async function link() {
    setBusy(true);
    setErr(null);
    try {
      await api.syncLink(cloudUrl.trim(), apiKey, org.trim());
      setCloudUrl("");
      setApiKey("");
      setOrg("");
      dropGitopsCache();
      onChange();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not link");
    } finally {
      setBusy(false);
    }
  }

  async function unlink() {
    setBusy(true);
    try {
      await api.syncUnlink();
      noteGitopsUnlinked();
      dropGitopsCache();
      onChange();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
      <div className="mb-3.5 flex items-center gap-2.5">
        <Link2 size={16} className="text-accent" />
        <div className="text-[14px] font-semibold">Cloud link</div>
        <span
          className={cn(
            "ml-auto flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 font-mono text-[10px] uppercase tracking-wide",
            status.linked
              ? "border-[rgba(95,208,122,0.28)] bg-[rgba(95,208,122,0.09)] text-st-done"
              : "border-line-2 text-faint",
          )}
        >
          <span className={cn("h-1.5 w-1.5 rounded-full", status.linked ? "bg-st-done" : "bg-faint")} />
          {status.linked ? "linked" : "not linked"}
        </span>
      </div>

      {status.linked ? (
        <>
          <div className="overflow-hidden rounded-[9px] border border-line bg-surface">
            <LinkRow label="Cloud URL" value={status.cloud_url} />
            {status.org && <LinkRow label="Tenant / org" value={status.org} />}
            <LinkRow
              label="Link key"
              value={status.credential_set ? "configured" : "missing"}
              valueClass={status.credential_set ? "text-accent" : "text-st-blocked"}
              note="scope: sync"
            />
            <LinkRow
              label="Managed by"
              value={status.source === "env" ? "env (SYNC_CLOUD_URL)" : "this deployment (web)"}
              note={status.linked_at ? `linked ${ago(status.linked_at)}` : ""}
              last
            />
          </div>

          <ProjectsTable projects={status.projects} scopedId={scopedId} onScope={onScope} />

          {status.source === "env" ? (
            <p className="mt-4 border-t border-line pt-3 text-[11px] leading-relaxed text-faint">
              This link comes from the <code className="font-mono text-[10.5px]">SYNC_CLOUD_URL</code> env
              var. Manage it where the instance is configured, or link from here to override it.
            </p>
          ) : (
            <div className="mt-4 flex items-center gap-3 border-t border-line pt-3.5">
              <Button variant="danger" size="sm" onClick={unlink} disabled={busy}>
                <Unlink size={13} />
                Unlink
              </Button>
              <span className="text-[11px] text-faint">
                Stops all sync. Local data is untouched; cloud items remain.
              </span>
            </div>
          )}
        </>
      ) : (
        <div className="space-y-3">
          <div>
            <Label>Cloud URL</Label>
            <Input value={cloudUrl} onChange={(e) => setCloudUrl(e.target.value)} placeholder="cloud.graphban.dev" />
          </div>
          <div>
            <Label>Link key</Label>
            <Input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="paste key…" />
          </div>
          <div>
            <Label>
              Tenant / org <span className="text-faint-2">(optional label)</span>
            </Label>
            <Input value={org} onChange={(e) => setOrg(e.target.value)} placeholder="acme" />
          </div>
          {err && <p className="text-[11px] text-st-blocked">{err}</p>}
          <Button size="sm" onClick={link} disabled={busy || !cloudUrl.trim() || !apiKey}>
            {busy ? "Linking…" : "Link instance"}
          </Button>
          <p className="text-[11px] leading-relaxed text-faint">
            The link key is stored encrypted at rest and never shown again — the same handling as
            your provider keys.
          </p>
        </div>
      )}
    </div>
  );
}

function ProjectsTable({
  projects,
  scopedId,
  onScope,
}: {
  projects: SyncProjectState[];
  scopedId: string | null;
  onScope: (id: string) => void;
}) {
  return (
    <div className="mt-4">
      <div className="mb-2 flex items-baseline gap-2.5">
        <span className="font-mono text-[10px] uppercase tracking-wide text-faint">Local projects → cloud org</span>
        <span className="font-mono text-[10px] text-faint-2">{projects.length} readable</span>
      </div>
      <div className="overflow-hidden rounded-[9px] border border-line bg-surface">
        <div className="flex items-center gap-3 border-b border-line bg-surface-2 px-3.5 py-1.5 font-mono text-[9.5px] uppercase tracking-wide text-faint-2">
          <span className="flex-1">Project</span>
          <span className="w-24">Graph</span>
          <span className="w-32">Sync</span>
          <span className="w-14" />
        </div>
        {projects.map((p) => {
          const meta = STATUS_META[p.status];
          const sel = p.project_id === scopedId;
          return (
            <button
              key={p.project_id}
              onClick={() => onScope(p.project_id)}
              className={cn(
                "flex w-full items-center gap-3 border-b border-line px-3.5 py-2 text-left transition-colors last:border-b-0 hover:bg-surface-2",
                sel && "bg-[rgba(198,242,78,0.05)] shadow-[inset_2px_0_0_0_var(--color-accent)]",
              )}
            >
              <span className="flex min-w-0 flex-1 items-center gap-2">
                <span className={cn("h-1.5 w-1.5 flex-none rounded-full", p.total_nodes ? "bg-st-done" : "bg-faint-2")} />
                <span className="truncate font-mono text-[12px] text-fg-2">{p.name}</span>
                {sel && (
                  <span className="flex-none rounded border border-accent/25 bg-[rgba(198,242,78,0.07)] px-1.5 py-px font-mono text-[9px] uppercase tracking-wide text-accent">
                    scoped
                  </span>
                )}
              </span>
              <span className="w-24 font-mono text-[10.5px] tracking-wide text-muted">
                {p.sync_graph ? `${p.total_nodes} nodes` : "kept local"}
              </span>
              <span className="flex w-32 items-center gap-1.5">
                <span className={cn("h-1.5 w-1.5 flex-none rounded-full", meta.dot)} />
                <span className={cn("font-mono text-[10.5px] tracking-wide", meta.cls)}>{meta.label}</span>
                {p.pending > 0 && p.sync_graph && (
                  <span className="font-mono text-[9.5px] text-faint-2">+{p.pending}</span>
                )}
              </span>
              <span className="w-14 text-right font-mono text-[9.5px] text-faint-2">
                {p.last_synced_at ? ago(p.last_synced_at) : ""}
              </span>
            </button>
          );
        })}
      </div>
      <p className="mt-2 text-[11px] leading-relaxed text-faint">
        Each row is a project on <span className="text-muted">this deployment</span> and its push
        status up to the linked cloud org. Select one to scope the controls below.
      </p>
    </div>
  );
}

// ── Scope bar ───────────────────────────────────────────────────────────────────────────────

function ScopeBar({ scoped, onClear }: { scoped: SyncProjectState | null; onClear: () => void }) {
  return (
    <div
      className={cn(
        "-mb-1 flex items-center gap-2.5 rounded-[9px] border px-3.5 py-2",
        scoped ? "border-accent/20 bg-[rgba(198,242,78,0.04)]" : "border-line-2 bg-surface",
      )}
    >
      <Boxes size={14} className={scoped ? "text-accent" : "text-faint-2"} />
      <span className="font-mono text-[10px] uppercase tracking-wide text-faint">Scope</span>
      <span className={cn("font-mono text-[12px]", scoped ? "text-fg-2" : "text-faint")}>
        {scoped ? scoped.name : "No project selected"}
      </span>
      <span className="ml-auto text-[11px] text-faint">
        {scoped ? "Controls below apply to this project only" : "Select a project above to enable the controls below"}
      </span>
      {scoped && (
        <button
          onClick={onClear}
          className="rounded-md border border-line-2 px-2 py-0.5 font-mono text-[9.5px] uppercase tracking-wide text-faint hover:border-line-hover hover:text-fg-2"
        >
          Clear
        </button>
      )}
    </div>
  );
}

// ── 2. Code-graph privacy ─────────────────────────────────────────────────────────────────────

function GraphPrivacyCard({
  scoped,
  linked,
  onChange,
}: {
  scoped: SyncProjectState | null;
  linked: boolean;
  onChange: () => void;
}) {
  const [busy, setBusy] = React.useState(false);
  const on = scoped?.sync_graph ?? true;
  const canWrite = !!scoped?.writable;

  async function toggle() {
    if (!scoped || !canWrite || busy) return;
    setBusy(true);
    try {
      await api.syncSetGraph(scoped.project_id, !on);
      onChange();
    } finally {
      setBusy(false);
    }
  }

  async function purge() {
    if (!scoped || !canWrite || busy) return;
    setBusy(true);
    try {
      await api.syncPurge(scoped.project_id);
      onChange();
    } finally {
      setBusy(false);
    }
  }

  return (
    <ScopedCard title="Code-graph privacy" scoped={scoped} icon={<Boxes size={16} className="text-accent" />}>
      <p className="mb-3 max-w-[62ch] text-[12.5px] leading-relaxed text-muted">
        The local instance summarizes your code graph and pushes it to the cloud so triage and
        collision-clustering can reason across the whole repo. Vectors are never sent — the cloud
        re-embeds from summaries.
      </p>

      <button
        onClick={toggle}
        disabled={!canWrite || busy}
        className={cn(
          "flex w-full items-start gap-2.5 rounded-[9px] border border-line bg-surface px-3 py-2.5 text-left transition-colors hover:border-line-hover disabled:cursor-not-allowed",
        )}
      >
        <span
          className={cn(
            "mt-px flex h-[15px] w-[15px] flex-none items-center justify-center rounded-[4px] border",
            on ? "border-accent bg-accent" : "border-line-3",
          )}
        >
          {on && <Check size={10} strokeWidth={3.5} className="text-bg" />}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-[12.5px] font-medium text-fg-2">
            Sync this project&rsquo;s code graph to the cloud.
          </span>
          <span className="mt-0.5 block text-[11px] text-faint">
            Summaries and structure only · re-embedded cloud-side
          </span>
        </span>
      </button>

      {!on && (
        <div className="mt-3 flex gap-2.5 rounded-[9px] border border-st-review/25 bg-[rgba(224,179,74,0.06)] px-3 py-2.5">
          <AlertTriangle size={15} className="mt-px flex-none text-st-review" />
          <span className="text-[12px] leading-relaxed text-st-review">
            Without a cloud code graph, triage clustering is far less effective — collisions can&rsquo;t
            be predicted from code structure.
          </span>
        </div>
      )}

      <div className="mt-3.5 flex items-center gap-3 border-t border-line pt-3.5">
        <Button variant="danger" size="sm" onClick={purge} disabled={!canWrite || !linked || busy}>
          <Trash2 size={13} />
          Purge cloud graph
        </Button>
        <span className="text-[11px] leading-relaxed text-faint">
          Removes this project&rsquo;s pushed graph from the cloud. Re-enabling sync re-pushes it.
        </span>
      </div>
    </ScopedCard>
  );
}

// ── 3. Code-graph push ────────────────────────────────────────────────────────────────────────

function GraphPushCard({
  scoped,
  linked,
  onChange,
}: {
  scoped: SyncProjectState | null;
  linked: boolean;
  onChange: () => void;
}) {
  const [busy, setBusy] = React.useState(false);
  const [note, setNote] = React.useState<string | null>(null);
  const stale = (scoped?.pending ?? 0) > 0;
  const canWrite = !!scoped?.writable;

  async function push() {
    if (!scoped || !canWrite || busy) return;
    setBusy(true);
    setNote(null);
    try {
      const r = await api.syncPush(scoped.project_id);
      setNote(r.skipped ? (r.reason ?? "skipped") : `pushed ${r.pushed ?? 0} · removed ${r.removed ?? 0}`);
      onChange();
    } catch (e) {
      setNote(e instanceof Error ? e.message : "push failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <ScopedCard
      title="Code-graph push"
      scoped={scoped}
      icon={<RefreshCw size={16} className="text-accent" />}
      pill={
        scoped && (
          <span
            className={cn(
              "ml-auto flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 font-mono text-[10px] uppercase tracking-wide",
              stale
                ? "border-st-review/28 bg-[rgba(224,179,74,0.08)] text-st-review"
                : "border-[rgba(95,208,122,0.28)] bg-[rgba(95,208,122,0.09)] text-st-done",
            )}
          >
            <span className={cn("h-1.5 w-1.5 rounded-full", stale ? "bg-st-review" : "bg-st-done")} />
            {stale ? "stale" : "in sync"}
          </span>
        )
      }
    >
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1 rounded-[9px] border border-line bg-surface px-3.5 py-2.5 font-mono text-[12px]">
        <Stat value={String(scoped?.total_nodes ?? 0)} label="nodes" />
        <Stat value={ago(scoped?.last_synced_at ?? null)} label="last synced" />
        <Stat
          value={String(scoped?.pending ?? 0)}
          label="pending"
          valueClass={stale ? "text-st-review" : "text-faint"}
        />
      </div>

      <div className="mt-3 flex items-center gap-3">
        <Button size="sm" onClick={push} disabled={!canWrite || !linked || busy}>
          <RefreshCw size={13} className={busy ? "animate-spin" : ""} />
          {busy ? "Syncing…" : "Push now"}
        </Button>
        <span className="text-[11px] leading-relaxed text-faint">
          {note ?? "Incremental — only changed nodes are sent; resumable if interrupted."}
        </span>
      </div>
    </ScopedCard>
  );
}

// ── 4. Portable bundle ────────────────────────────────────────────────────────────────────────

function PortableBundleCard({
  scoped,
  onChange,
}: {
  scoped: SyncProjectState | null;
  onChange: () => void;
}) {
  const fileRef = React.useRef<HTMLInputElement>(null);
  const [bundle, setBundle] = React.useState<{ name: string; nodes: unknown[]; edges: unknown[] } | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [note, setNote] = React.useState<string | null>(null);
  const canWrite = !!scoped?.writable;

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-selecting the same file
    if (!file) return;
    setNote(null);
    try {
      const parsed = JSON.parse(await file.text());
      setBundle({ name: file.name, nodes: parsed.nodes ?? [], edges: parsed.edges ?? [] });
    } catch {
      setNote("Not a valid JSON bundle");
    }
  }

  async function importBundle() {
    if (!scoped || !bundle || !canWrite || busy) return;
    setBusy(true);
    setNote(null);
    try {
      const r = await api.syncImport(scoped.project_id, bundle, false);
      setNote(`imported ${r.nodes_upserted} nodes · ${r.edges_upserted} edges`);
      setBundle(null);
      onChange();
    } catch (e) {
      setNote(e instanceof Error ? e.message : "import failed");
    } finally {
      setBusy(false);
    }
  }

  async function exportBundle() {
    if (!scoped || busy) return;
    setBusy(true);
    try {
      const data = await api.syncExport(scoped.project_id);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${scoped.project_id}-code-graph.json`;
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setBusy(false);
    }
  }

  return (
    <ScopedCard
      title="Portable bundle"
      scoped={scoped}
      icon={<Package size={16} className="text-purple" />}
      badge={
        <span className="ml-auto rounded-full border border-purple/25 bg-[rgba(167,139,250,0.08)] px-2.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-purple-2">
          secondary transport
        </span>
      }
    >
      <p className="mb-3 text-[12.5px] leading-relaxed text-muted">
        No direct connection? Move the graph as an export file — the target re-embeds on import.
      </p>

      <input ref={fileRef} type="file" accept="application/json,.json" onChange={onFile} className="hidden" />
      <button
        onClick={() => canWrite && fileRef.current?.click()}
        disabled={!canWrite}
        className={cn(
          "flex w-full flex-col items-center justify-center gap-2 rounded-[10px] border border-dashed border-line-3 bg-surface px-4 py-6 transition-colors hover:border-accent/45 disabled:cursor-not-allowed disabled:hover:border-line-3",
        )}
      >
        <Upload size={20} className="text-faint" />
        <span className="text-[12.5px] text-muted-2">
          Click to choose an export bundle <span className="text-accent">(.json)</span>
        </span>
        <span className="font-mono text-[10px] uppercase tracking-wide text-faint-2">
          Re-embedded on import
        </span>
      </button>

      {bundle && (
        <div className="mt-3 flex items-center gap-2.5 rounded-[9px] border border-line bg-surface px-3 py-2.5">
          <Check size={14} className="flex-none text-st-done" />
          <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-muted-2">
            {bundle.name} · {bundle.nodes.length} nodes · {bundle.edges.length} edges
          </span>
          <button
            onClick={() => setBundle(null)}
            className="flex-none rounded-md border border-line-2 px-2 py-0.5 font-mono text-[9.5px] uppercase tracking-wide text-faint hover:border-st-blocked/35 hover:text-st-blocked"
          >
            Clear
          </button>
          <Button size="sm" onClick={importBundle} disabled={busy}>
            {busy ? "Importing…" : "Import"}
          </Button>
        </div>
      )}

      <div className="mt-3 flex items-center gap-3">
        <Button variant="outline" size="sm" onClick={exportBundle} disabled={busy}>
          <Download size={13} />
          Export bundle
        </Button>
        <span className="text-[11px] text-faint">
          {note ?? `${scoped?.total_nodes ?? 0} nodes · vector-free`}
        </span>
      </div>
    </ScopedCard>
  );
}

// ── shared bits ───────────────────────────────────────────────────────────────────────────────

function ScopedCard({
  title,
  icon,
  scoped,
  pill,
  badge,
  children,
}: {
  title: string;
  icon: React.ReactNode;
  scoped: SyncProjectState | null;
  pill?: React.ReactNode;
  badge?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "rounded-[13px] border border-line-2 bg-surface-2 p-4 transition-opacity",
        !scoped && "pointer-events-none opacity-40",
      )}
    >
      <div className="mb-3 flex items-center gap-2.5">
        {icon}
        <div className="text-[14px] font-semibold">{title}</div>
        <span className="rounded border border-line bg-surface px-1.5 py-0.5 font-mono text-[10px] tracking-wide text-muted">
          {scoped ? scoped.name : "—"}
        </span>
        {pill}
        {badge}
      </div>
      {children}
    </div>
  );
}

function LinkRow({
  label,
  value,
  note,
  valueClass,
  last,
}: {
  label: string;
  value: string;
  note?: string;
  valueClass?: string;
  last?: boolean;
}) {
  return (
    <div className={cn("flex items-baseline gap-3.5 px-3.5 py-2.5", !last && "border-b border-line")}>
      <span className="w-32 flex-none font-mono text-[10px] uppercase tracking-wide text-faint">{label}</span>
      <span className={cn("min-w-0 flex-1 break-words font-mono text-[12px]", valueClass ?? "text-fg-2")}>{value}</span>
      {note && <span className="font-mono text-[10px] text-faint-2">{note}</span>}
    </div>
  );
}

function Stat({ value, label, valueClass }: { value: string; label: string; valueClass?: string }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className={cn("font-medium", valueClass ?? "text-fg-2")}>{value}</span>
      <span className="text-faint">{label}</span>
    </span>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">{children}</div>;
}
