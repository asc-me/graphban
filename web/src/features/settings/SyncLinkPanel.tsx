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
  Upload,
} from "lucide-react";
import * as React from "react";

import { FETCH_FAILED, PlannerError } from "@/components/planner/PlannerStates";
import { Button } from "@/components/ui/button";
import { LinkedPanel } from "@/features/organization/LinkedPanel";
import { LinkToOrgWizard, useLinkFlow } from "@/features/organization/LinkToOrgWizard";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useSyncBudget, useSyncLocal, useSyncOrg, useSyncStatus } from "@/lib/queries";
import type { LocalSyncProject, SyncBudget, SyncProjectState, SyncProjectStatus } from "@/lib/types";
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
  const statusQ = useSyncStatus();
  const { data: status, isLoading } = statusQ;
  const [params] = useSearchParams();
  const reason = params.get("reason");
  const qc = useQueryClient();
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["sync-status"] });
    qc.invalidateQueries({ queryKey: ["platform"] });
  };
  const [scopedId, setScopedId] = React.useState<string | null>(null);

  const scoped = status?.projects.find((p) => p.project_id === scopedId) ?? null;

  // PRD-47 S17. `isLoading || !status` was the whole guard, so a failed read sat at
  // "Loading sync status…" forever — the same absence-as-clean-result as Project Home's
  // dependencies with the opposite polarity: a failure that reads as a promise still
  // outstanding, and no way to retry it. Checked before the loading branch because
  // `isLoading` is false once the query has errored, but `!status` never becomes false.
  if (statusQ.isError && !status) {
    return (
      <PlannerError
        message={`${FETCH_FAILED} Whether this instance is linked was not read — this is not an unlinked instance.`}
        onRetry={() => void statusQ.refetch()}
      />
    );
  }

  if (isLoading || !status) return <p className="text-body text-faint">Loading sync status…</p>;

  return (
    <div className="max-w-2xl space-y-6">
      <div>
        <h2 className="text-[15px] font-semibold tracking-tight">Sync / Link</h2>
        <p className="mt-1 max-w-[60ch] text-body leading-relaxed text-muted">
          Connect this self-hosted instance to the Graphban console. Sign in with your own
          account — no key to copy. This box builds the graph; the console holds items, claims, and
          memory. Vectors never leave the box; the console re-embeds.
        </p>
        {reason === "missing" && (
          <p className="mt-3 rounded-[11px] border border-st-review/30 bg-st-review/[0.06] px-3 py-2 text-body text-st-review">
            No cloud URL is linked. Connect a tenant here rather than opening a blank address.
          </p>
        )}
        {reason === "malformed" && (
          <p className="mt-3 rounded-[11px] border border-st-review/30 bg-st-review/[0.06] px-3 py-2 text-body text-st-review">
            The stored cloud URL is not a usable http(s) address, so it was not opened.
          </p>
        )}
      </div>

      <CloudLinkCard status={status} scopedId={scopedId} onScope={setScopedId} />
      <ContributionCard status={status} onChange={invalidate} />

      <ScopeBar scoped={scoped} onClear={() => setScopedId(null)} />

      <GraphPrivacyCard scoped={scoped} linked={status.linked} onChange={invalidate} />
      <GraphPushCard scoped={scoped} linked={status.linked} onChange={invalidate} />
      <PortableBundleCard scoped={scoped} onChange={invalidate} />
    </div>
  );
}

function ContributionCard({
  status,
  onChange,
}: {
  status: NonNullable<ReturnType<typeof useSyncStatus>["data"]>;
  onChange: () => void;
}) {
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);
  const on = Boolean(status.telemetry_share);

  async function toggle() {
    setBusy(true);
    setErr(null);
    try {
      await api.setTelemetryShare(!on);
      onChange();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const floors = status.last_floors;
  return (
    <div data-testid="sync-contribution" className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
      <h3 className="text-[13px] font-medium">Capability contribution</h3>
      <p className="mt-1 text-body text-muted">
        When on, this instance posts capability rollups nightly over the deployment-sync
        credential — the D11 field set only, never a path, item or reviewer. Resist
        casual re-identification, never anonymous. Opting out stops posting and the
        hosted recompute drops this contributor.
      </p>
      <button
        type="button"
        data-testid="sync-contribution-toggle"
        className="mt-2 rounded-[8px] border border-control px-2 py-1 text-[12px]"
        disabled={busy}
        onClick={() => void toggle()}
      >
        {on ? "Sharing rollups" : "Not sharing"}
      </button>
      {err && <p className="mt-2 text-[12px] text-st-review">{err}</p>}
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 font-mono text-small text-faint">
        <dt>last contribution</dt>
        <dd data-testid="sync-contribution-last">
          {status.last_contribution_at ?? "never"}
        </dd>
        <dt>row count</dt>
        <dd data-testid="sync-contribution-rows">
          {status.last_contribution_rows ?? "—"}
        </dd>
        <dt>floors</dt>
        <dd data-testid="sync-contribution-floors">
          {floors
            ? floors.cleared
              ? "cleared"
              : floors.reason || "not cleared"
            : "—"}
        </dd>
        {status.last_snapshot_at && (
          <>
            <dt>snapshot</dt>
            <dd>{status.last_snapshot_at}</dd>
          </>
        )}
      </dl>
    </div>
  );
}

const PERMISSIONS: { value: string; label: string }[] = [
  { value: "read_only", label: "read-only" },
  { value: "comments", label: "comments only" },
  { value: "proposals", label: "proposals" },
];

function pushWord(result: LocalSyncProject["last_push_result"]): string {
  if (result === "ok") return "ok";
  if (result === "refused") return "refused";
  if (result === "unknown") return "unknown";
  return "not pushed";
}

/**
 * Per-project sync level, permission, queue and the box budget (GRPH-1739).
 * Rendered only once the wizard is closed, so Finish in background lands here.
 * A missing `local` is "the read did not happen", not an empty project list.
 */
function ProjectSyncCard({
  statusProjects,
  readOnly,
}: {
  statusProjects: SyncProjectState[];
  readOnly: boolean;
}) {
  const qc = useQueryClient();
  const localQ = useSyncLocal();
  const budgetQ = useSyncBudget();
  const names = new Map(statusProjects.map((p) => [p.project_id, p.name]));
  const [bytes, setBytes] = React.useState("");
  const [requests, setRequests] = React.useState("");
  const [budgetErr, setBudgetErr] = React.useState("");

  React.useEffect(() => {
    if (!budgetQ.data) return;
    setBytes(String(budgetQ.data.bytes_per_min));
    setRequests(String(budgetQ.data.requests_per_min));
  }, [budgetQ.data]);

  async function saveLevel(id: string, sync_level: string) {
    try {
      await api.putProjectSync(id, { sync_level });
      await qc.invalidateQueries({ queryKey: ["sync-local"] });
    } catch {
      /* the next read shows the level the server kept */
    }
  }
  async function savePermission(id: string, cloud_permission: string) {
    await api.putProjectSync(id, { cloud_permission });
    await qc.invalidateQueries({ queryKey: ["sync-local"] });
  }
  async function retry(id: string) {
    await api.retryParked(id);
    await qc.invalidateQueries({ queryKey: ["sync-local"] });
  }
  async function saveBudget() {
    setBudgetErr("");
    const body: SyncBudget = { bytes_per_min: Number(bytes), requests_per_min: Number(requests) };
    if (!Number.isFinite(body.bytes_per_min) || !Number.isFinite(body.requests_per_min)) {
      setBudgetErr("Enter both limits.");
      return;
    }
    try {
      await api.putSyncBudget(body);
      await qc.invalidateQueries({ queryKey: ["sync-budget"] });
    } catch (e) {
      setBudgetErr(e instanceof Error ? e.message : "Could not save the budget.");
    }
  }

  const body = localQ.data;
  const unread = localQ.isError || body == null || body.local == null;

  return (
    <div className="mt-4 border-t border-line pt-3" data-testid="project-sync">
      <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">Project sync</div>
      {localQ.isLoading && <p className="text-small text-faint">Reading project sync…</p>}
      {!localQ.isLoading && unread && (
        <p className="text-small text-faint">Project sync was not read.</p>
      )}
      {!localQ.isLoading && body?.local && body.local.length === 0 && (
        <p className="text-small text-faint">No local projects.</p>
      )}
      {body?.local?.map((row) => {
        const name = names.get(row.project_id) ?? row.project_id;
        return (
          <div key={row.project_id} className="mb-3 space-y-1 rounded-[8px] border border-line px-3 py-2">
            <div className="font-mono text-[12px] text-fg-2">{name}</div>
            <fieldset className="flex flex-wrap gap-2" aria-label={`Sync level for ${name}`}>
              {([
                ["sync", "Sync to org"],
                ["digests", "Digests only"],
                ["local", "Local only"],
              ] as const).map(([level, label]) => (
                <label key={level} className="flex items-center gap-1 text-small">
                  <input
                    type="radio"
                    name={`settings-level-${row.project_id}`}
                    checked={row.sync_level === level}
                    disabled={readOnly}
                    onChange={() => void saveLevel(row.project_id, level)}
                  />
                  {label}
                </label>
              ))}
            </fieldset>
            <label className="flex items-center gap-2 text-small">
              <span className="text-faint">Permission</span>
              <select
                aria-label={`Cloud permission for ${name}`}
                value={row.cloud_permission ?? "comments"}
                disabled={readOnly || row.sync_level === "local"}
                onChange={(e) => void savePermission(row.project_id, e.target.value)}
                className="h-8 rounded-[8px] border border-control bg-surface-2 px-2 text-[12px]"
              >
                {PERMISSIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </label>
            <p className="font-mono text-[10px] text-muted">
              {row.backfill.confirmed} of {row.backfill.total} · queued {row.queued}
              {" · "}parked {row.parked.count}
              {row.parked.reasons.length ? ` (${row.parked.reasons.join(", ")})` : ""}
              {" · "}last push {pushWord(row.last_push_result)}
              {row.held_by ? ` · held by ${row.held_by}` : ""}
            </p>
            <Button size="sm" variant="ghost" disabled={readOnly} onClick={() => void retry(row.project_id)}>
              Retry now
            </Button>
          </div>
        );
      })}
      <div className="mt-2 space-y-1">
        <div className="font-mono text-[10px] uppercase tracking-wide text-faint">Box budget</div>
        {budgetQ.isLoading && <p className="text-small text-faint">Reading budget…</p>}
        {!budgetQ.isLoading && (budgetQ.isError || budgetQ.data == null) && (
          <p className="text-small text-faint">Budget was not read.</p>
        )}
        {budgetQ.data && (
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-small">
              <span className="mb-1 block text-faint">Bytes per minute</span>
              <input
                aria-label="Bytes per minute"
                value={bytes}
                disabled={readOnly}
                onChange={(e) => setBytes(e.target.value)}
                className="h-8 w-28 rounded-[8px] border border-control bg-surface-2 px-2 font-mono text-[12px]"
              />
            </label>
            <label className="text-small">
              <span className="mb-1 block text-faint">Requests per minute</span>
              <input
                aria-label="Requests per minute"
                value={requests}
                disabled={readOnly}
                onChange={(e) => setRequests(e.target.value)}
                className="h-8 w-20 rounded-[8px] border border-control bg-surface-2 px-2 font-mono text-[12px]"
              />
            </label>
            <Button size="sm" variant="ghost" disabled={readOnly} onClick={() => void saveBudget()}>
              Save budget
            </Button>
          </div>
        )}
        {budgetErr && <p className="text-small text-st-blocked">{budgetErr}</p>}
      </div>
    </div>
  );
}

// ── 1. Cloud link ──────────────────────────────────────────────────────────────────────────

function CloudLinkCard({
  status,
  scopedId,
  onScope,
}: {
  status: NonNullable<ReturnType<typeof useSyncStatus>["data"]>;
  scopedId: string | null;
  onScope: (id: string) => void;
}) {
  const { data: orgOverview } = useSyncOrg(status.linked);
  // /sync/org's can_unlink is the read-only fallback for an older API without can_link.
  const flow = useLinkFlow(status, status.linked && orgOverview?.can_unlink === false);

  return (
    <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
      <div className="mb-3.5 flex items-center gap-2.5">
        <Link2 size={16} className="text-accent" />
        <div className="text-lead font-semibold">Cloud link</div>
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

      {flow.wizard ? (
        <LinkToOrgWizard
          status={status}
          initialStep={flow.initialStep}
          readOnly={flow.readOnly}
          onLinked={flow.onLinked}
          onDone={flow.onDone}
        />
      ) : (
        <>
          <LinkedPanel
            status={status}
            readOnly={flow.readOnly}
            unlinkable={status.source !== "env"}
            busy={flow.busy}
            orgHref={status.cloud_url || undefined}
            onChooseProjects={() => flow.openAt(3)}
            onSignIn={() => flow.openAt(2)}
            onUnlink={() => void flow.unlink()}
          />
          <ProjectsTable projects={status.projects} scopedId={scopedId} onScope={onScope} />
          <ProjectSyncCard statusProjects={status.projects} readOnly={flow.readOnly} />
          {status.source === "env" && (
            <p className="mt-4 border-t border-line pt-3 text-small leading-relaxed text-faint">
              This link comes from the <code className="font-mono text-[10.5px]">SYNC_CLOUD_URL</code> env
              var. Manage it where the instance is configured.
            </p>
          )}
        </>
      )}
    </div>
  );
}

/**
 * Local projects → the linked cloud org, one row each; selecting a row scopes the cards
 * below it (PRD-47 S17).
 *
 * The design's row also carries **attach / detach**, and they are deliberately not here:
 * the only primitive the backend exposes is `syncSetGraph` (`PATCH /platform`), which
 * toggles graph push for a project that is ALREADY mapped. Nothing attaches a project to
 * the org or detaches it, so a control here would be a button that cannot work. It belongs
 * with the endpoint, not with this panel — GRPH-986.
 */
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
        <div className="flex items-center gap-3 border-b border-line bg-surface-2 px-3.5 py-1.5 font-mono text-meta uppercase tracking-wide text-faint-2">
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
                "flex w-full items-center gap-3 border-b border-control px-3.5 py-2 text-left transition-colors last:border-b-0 hover:bg-surface-2",
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
              <span className="w-32 whitespace-nowrap font-mono text-[10.5px] tracking-wide text-muted">
                {p.sync_graph ? `${p.total_nodes} nodes` : "graph kept local"}
              </span>
              <span className="flex w-32 items-center gap-1.5">
                <span className={cn("h-1.5 w-1.5 flex-none rounded-full", meta.dot)} />
                <span className={cn("font-mono text-[10.5px] tracking-wide", meta.cls)}>{meta.label}</span>
                {p.pending > 0 && p.sync_graph && (
                  <span className="font-mono text-meta text-faint-2">+{p.pending}</span>
                )}
              </span>
              <span className="w-14 text-right font-mono text-meta text-faint-2">
                {p.last_synced_at ? ago(p.last_synced_at) : ""}
              </span>
            </button>
          );
        })}
      </div>
      <p className="mt-2 text-small leading-relaxed text-faint">
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
      <span className="ml-auto text-small text-faint">
        {scoped ? "Controls below apply to this project only" : "Select a project above to enable the controls below"}
      </span>
      {scoped && (
        <button
          onClick={onClear}
          className="rounded-md border border-control px-2 py-0.5 font-mono text-meta uppercase tracking-wide text-faint hover:border-control-hover hover:text-fg-2"
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
      <p className="mb-3 max-w-[62ch] text-body leading-relaxed text-muted">
        The local instance summarizes your code graph and pushes it to the cloud so triage and
        collision-clustering can reason across the whole repo. Vectors are never sent — the cloud
        re-embeds from summaries.
      </p>

      <button
        onClick={toggle}
        disabled={!canWrite || busy}
        className={cn(
          "flex w-full items-start gap-2.5 rounded-[9px] border border-control bg-surface px-3 py-2.5 text-left transition-colors hover:border-control-hover disabled:cursor-not-allowed",
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
          <span className="block text-body font-medium text-fg-2">
            Sync this project&rsquo;s code graph to the cloud.
          </span>
          <span className="mt-0.5 block text-small text-faint">
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
        <span className="text-small leading-relaxed text-faint">
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
        <span className="text-small leading-relaxed text-faint">
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
      <p className="mb-3 text-body leading-relaxed text-muted">
        No direct connection? Move the graph as an export file — the target re-embeds on import.
      </p>

      <input ref={fileRef} type="file" accept="application/json,.json" onChange={onFile} className="hidden" />
      <button
        onClick={() => canWrite && fileRef.current?.click()}
        disabled={!canWrite}
        className={cn(
          "flex w-full flex-col items-center justify-center gap-2 rounded-[10px] border border-dashed border-control-hover bg-surface px-4 py-6 transition-colors hover:border-accent/45 disabled:cursor-not-allowed disabled:hover:border-control-hover",
        )}
      >
        <Upload size={20} className="text-faint" />
        <span className="text-body text-muted-2">
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
            className="flex-none rounded-md border border-control px-2 py-0.5 font-mono text-meta uppercase tracking-wide text-faint hover:border-st-blocked/35 hover:text-st-blocked"
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
        <span className="text-small text-faint">
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
        <div className="text-lead font-semibold">{title}</div>
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

function Stat({ value, label, valueClass }: { value: string; label: string; valueClass?: string }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className={cn("font-medium", valueClass ?? "text-fg-2")}>{value}</span>
      <span className="text-faint">{label}</span>
    </span>
  );
}
