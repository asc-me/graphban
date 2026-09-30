import { Check, Eye, RotateCcw, Search, X } from "lucide-react";
import * as React from "react";

import { MemoryReviewSkeleton, PlannerError } from "@/components/planner/PlannerStates";
import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";
import { useProjectCtx } from "@/features/ProjectContext";
import {
  useAutoActions,
  useCandidateClusters,
  useCandidateShards,
  useJudgeShard,
  usePromoteCluster,
  useReviewShard,
  useScoredCandidates,
  useShards,
  useUndoAutoShard,
} from "@/lib/queries";
import type { ScoredCandidate, Shard, ShardCluster } from "@/lib/types";
import { UNVETTED_SOURCES } from "./MemoryReviewView";

type QueueId = "needs" | "conflicts" | "dupes" | "stale" | "unvetted";

const QUEUES: { id: QueueId; label: string; hint: string }[] = [
  { id: "needs", label: "Needs review", hint: "Candidates awaiting a human decision" },
  { id: "conflicts", label: "Conflicts", hint: "Contradict published memory" },
  { id: "dupes", label: "Duplicates", hint: "Near-duplicates of another candidate" },
  { id: "stale", label: "Stale", hint: "Auto-acted over 7 days ago, still unreviewed" },
  { id: "unvetted", label: "Unvetted", hint: "Published without a human looking" },
];

const PAGE_SIZE = 50;
const SWEEP_THRESHOLD = 0.3;
const STALE_DAYS = 7;

function isStale(dateStr: string): boolean {
  if (!dateStr) return false;
  const d = new Date(dateStr);
  if (Number.isNaN(d.getTime())) return false;
  return Date.now() - d.getTime() > STALE_DAYS * 86_400_000;
}

function assignQueue(
  shard: Shard,
  score: ScoredCandidate | undefined,
): QueueId {
  if (score?.duplicate_of) return "dupes";
  if ((score?.conflicts?.length ?? 0) > 0) return "conflicts";
  if (shard.scoring_source && shard.status !== "candidate") {
    if (UNVETTED_SOURCES.includes(shard.scoring_source)) return "unvetted";
    if (isStale(shard.created_at)) return "stale";
  }
  return "needs";
}

interface TriagedShard {
  shard: Shard;
  score?: ScoredCandidate;
  queue: QueueId;
}

function buildTriageRows(
  candidates: Shard[],
  autoActions: Shard[],
  scored: ScoredCandidate[],
): TriagedShard[] {
  const scoreById = new Map(scored.map((s) => [s.shard.id, s]));
  const seen = new Set<string>();
  const rows: TriagedShard[] = [];

  for (const c of candidates) {
    if (seen.has(c.id)) continue;
    seen.add(c.id);
    rows.push({ shard: c, score: scoreById.get(c.id), queue: assignQueue(c, scoreById.get(c.id)) });
  }
  for (const a of autoActions) {
    if (seen.has(a.id)) continue;
    seen.add(a.id);
    rows.push({ shard: a, score: scoreById.get(a.id), queue: assignQueue(a, scoreById.get(a.id)) });
  }
  return rows;
}

type SortKey = "newest" | "oldest" | "confidence";

interface ToastState {
  message: string;
  undoableIds: string[];
  visible: boolean;
}

export function MemoryTriageView() {
  const { activeId, active } = useProjectCtx();
  const judgeOn = Boolean(active?.memory_llm_judge);

  const { data: candidates, isPending, isError, error, refetch } = useCandidateShards(activeId);
  const { data: autoActions } = useAutoActions(activeId);
  const { data: scored } = useScoredCandidates(activeId);
  const { data: clusters } = useCandidateClusters(activeId);
  const { data: published } = useShards(activeId, 200);
  const review = useReviewShard();
  const undoAuto = useUndoAutoShard();
  const promoteCluster = usePromoteCluster();
  const judge = useJudgeShard();

  const [queue, setQueue] = React.useState<QueueId>("needs");
  const [search, setSearch] = React.useState("");
  const [sort, setSort] = React.useState<SortKey>("newest");
  const [facet, setFacet] = React.useState<string | null>(null);
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [cursor, setCursor] = React.useState(0);
  const [visibleCount, setVisibleCount] = React.useState(PAGE_SIZE);
  const [detailId, setDetailId] = React.useState<string | null>(null);
  const [sweepOpen, setSweepOpen] = React.useState(false);
  const [toast, setToast] = React.useState<ToastState>({ message: "", undoableIds: [], visible: false });
  const toastTimer = React.useRef<ReturnType<typeof setTimeout>>(null);

  const awaiting = isPending && candidates === undefined;
  const failed = isError || (candidates === undefined && !isPending);

  const allRows = React.useMemo(
    () => buildTriageRows(candidates ?? [], autoActions ?? [], scored ?? []),
    [candidates, autoActions, scored],
  );

  const publishedById = React.useMemo(
    () => new Map((published ?? []).filter((s) => s.status === "published").map((s) => [s.id, s])),
    [published],
  );

  const queueCounts = React.useMemo(() => {
    const counts: Record<QueueId, number> = { needs: 0, conflicts: 0, dupes: 0, stale: 0, unvetted: 0 };
    for (const r of allRows) counts[r.queue]++;
    return counts;
  }, [allRows]);

  const filtered = React.useMemo(() => {
    let rows = allRows.filter((r) => r.queue === queue);
    if (search.trim()) {
      const q = search.toLowerCase();
      rows = rows.filter((r) => r.shard.text.toLowerCase().includes(q) || r.shard.source.toLowerCase().includes(q));
    }
    if (facet) {
      rows = rows.filter((r) => r.shard.origin === facet || r.shard.scoring_source === facet);
    }
    rows.sort((a, b) => {
      if (sort === "oldest") return (a.shard.created_at || "").localeCompare(b.shard.created_at || "");
      if (sort === "confidence") return (b.score?.confidence ?? 0) - (a.score?.confidence ?? 0);
      return (b.shard.created_at || "").localeCompare(a.shard.created_at || "");
    });
    return rows;
  }, [allRows, queue, search, facet, sort]);

  const visible = filtered.slice(0, visibleCount);
  const detail = detailId ? allRows.find((r) => r.shard.id === detailId) : null;

  const facets = React.useMemo(() => {
    const origins = new Map<string, number>();
    for (const r of allRows) {
      const key = r.shard.origin || "unknown";
      origins.set(key, (origins.get(key) ?? 0) + 1);
    }
    return [...origins.entries()].sort((a, b) => b[1] - a[1]);
  }, [allRows]);

  const sweepTargets = React.useMemo(
    () => allRows.filter((r) => (r.score?.confidence ?? 0) < SWEEP_THRESHOLD && r.score != null),
    [allRows],
  );

  function showToast(message: string, undoableIds: string[]) {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast({ message, undoableIds, visible: true });
    toastTimer.current = setTimeout(() => setToast((t) => ({ ...t, visible: false })), 5000);
  }

  async function handleUndo(ids: string[]) {
    const results = await Promise.allSettled(ids.map((id) => undoAuto.mutateAsync(id)));
    const ok = results.filter((r) => r.status === "fulfilled").length;
    showToast(`Undid ${ok} of ${ids.length}`, []);
    setSelected(new Set());
  }

  async function handleBulkPublish(ids: string[]) {
    const results = await Promise.allSettled(ids.map((id) => review.publish.mutateAsync(id)));
    const ok = results.filter((r) => r.status === "fulfilled").length;
    const failedCount = ids.length - ok;
    const msg = failedCount > 0 ? `Published ${ok}, ${failedCount} failed` : `Published ${ok}`;
    showToast(msg, []);
    setSelected(new Set());
  }

  async function handleBulkReject(ids: string[]) {
    const results = await Promise.allSettled(ids.map((id) => review.reject.mutateAsync(id)));
    const ok = results.filter((r) => r.status === "fulfilled").length;
    const failedCount = ids.length - ok;
    const msg = failedCount > 0 ? `Rejected ${ok}, ${failedCount} failed` : `Rejected ${ok}`;
    showToast(msg, []);
    setSelected(new Set());
  }

  async function handleSweep() {
    const ids = sweepTargets.map((r) => r.shard.id);
    const results = await Promise.allSettled(ids.map((id) => review.reject.mutateAsync(id)));
    const ok = results.filter((r) => r.status === "fulfilled").length;
    const failedCount = ids.length - ok;
    const msg = failedCount > 0 ? `Swept ${ok}, ${failedCount} failed` : `Swept ${ok} low-confidence shards`;
    showToast(msg, ids);
    setSweepOpen(false);
    setSelected(new Set());
  }

  function toggleSelect(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  function toggleSelectAll() {
    if (selected.size === visible.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(visible.map((r) => r.shard.id)));
    }
  }

  function moveCursor(delta: number) {
    setCursor((c) => Math.max(0, Math.min(visible.length - 1, c + delta)));
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return;
    switch (e.key) {
      case "j": e.preventDefault(); moveCursor(1); break;
      case "k": e.preventDefault(); moveCursor(-1); break;
      case "x": e.preventDefault(); if (visible[cursor]) toggleSelect(visible[cursor].shard.id); break;
      case "Enter":
        e.preventDefault();
        if (visible[cursor]) setDetailId(visible[cursor].shard.id);
        break;
      case "Escape":
        e.preventDefault();
        setDetailId(null);
        break;
    }
  }

  React.useEffect(() => {
    setVisibleCount(PAGE_SIZE);
    setCursor(0);
    setSelected(new Set());
  }, [queue, search, facet, sort]);

  if (awaiting) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <PlaceHeader viewName="Memory triage" purpose="Triage the memory queue — route, sweep, and resolve candidates." />
        <div className="min-h-0 flex-1 overflow-y-auto"><MemoryReviewSkeleton /></div>
      </div>
    );
  }

  if (failed || !candidates) {
    const msg = error instanceof Error && error.message.includes("timed out")
      ? "Memory triage is taking too long to load — retry, or check the server."
      : "Memory triage unavailable";
    return (
      <div className="flex h-full min-h-0 flex-col">
        <PlaceHeader viewName="Memory triage" purpose="Triage the memory queue — route, sweep, and resolve candidates." />
        <PlannerError message={msg} onRetry={() => refetch()} />
      </div>
    );
  }

  const total = allRows.length;
  const allSelected = visible.length > 0 && selected.size === visible.length;

  return (
    <div className="flex h-full min-h-0 flex-col" onKeyDown={handleKeyDown}>
      <PlaceHeader
        viewName="Memory triage"
        purpose="Route candidates, sweep low-confidence auto-actions, and pick canonical wording for near-duplicates."
        action={
          <div className="flex items-center gap-3 text-[12px] text-muted">
            <span>{total} total</span>
            {sweepTargets.length > 0 && (
              <button
                onClick={() => setSweepOpen(true)}
                className="inline-flex items-center gap-1 rounded-md border border-control px-2 py-1 text-[11.5px] text-muted transition-colors hover:border-control-hover hover:text-ink"
              >
                <Eye size={12} /> Sweep {sweepTargets.length} low-confidence
              </button>
            )}
          </div>
        }
      />

      <div className="flex flex-none flex-wrap items-center gap-2 border-b border-line px-5 py-2">
        {QUEUES.map((q) => (
          <button
            key={q.id}
            onClick={() => setQueue(q.id)}
            title={q.hint}
            className={cn(
              "rounded-md border px-2 py-1 text-[11.5px] transition-colors",
              queue === q.id
                ? "border-fg bg-fg/5 text-fg"
                : "border-control text-muted hover:border-control-hover hover:text-ink",
            )}
          >
            {q.label}
            <span className="ml-1 text-faint">({queueCounts[q.id]})</span>
          </button>
        ))}

        <div className="ml-auto flex items-center gap-2">
          <div className="relative">
            <Search size={13} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-faint" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search…"
              className="rounded-md border border-control bg-surface py-1 pl-7 pr-2 text-[12px] text-ink placeholder:text-faint focus:border-control-hover focus:outline-none"
            />
          </div>
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as SortKey)}
            className="rounded-md border border-control bg-surface px-2 py-1 text-[12px] text-ink focus:border-control-hover focus:outline-none"
          >
            <option value="newest">Newest</option>
            <option value="oldest">Oldest</option>
            <option value="confidence">Confidence</option>
          </select>
        </div>
      </div>

      {facets.length > 1 && (
        <div className="flex flex-none flex-wrap items-center gap-1 border-b border-line px-5 py-1.5">
          {facets.map(([origin, count]) => (
            <button
              key={origin}
              onClick={() => setFacet(facet === origin ? null : origin)}
              className={cn(
                "rounded-md border px-1.5 py-0.5 text-[10.5px] transition-colors",
                facet === origin
                  ? "border-fg bg-fg/5 text-fg"
                  : "border-control text-faint hover:border-control-hover hover:text-muted",
              )}
            >
              {origin} ({count})
            </button>
          ))}
        </div>
      )}

      {selected.size > 0 && (
        <div className="flex flex-none items-center gap-2 border-b border-line bg-surface-2 px-5 py-1.5">
          <span className="text-[11.5px] text-muted">{selected.size} selected</span>
          <button
            onClick={() => handleBulkPublish([...selected])}
            disabled={review.publish.isPending}
            className="rounded-md border border-st-done/30 bg-st-done/10 px-2 py-0.5 text-[11px] text-st-done hover:bg-st-done/15 disabled:opacity-50"
          >
            Publish all
          </button>
          <button
            onClick={() => handleBulkReject([...selected])}
            disabled={review.reject.isPending}
            className="rounded-md border border-control px-2 py-0.5 text-[11px] text-muted hover:border-control-hover hover:text-ink disabled:opacity-50"
          >
            Reject all
          </button>
          <button
            onClick={() => setSelected(new Set())}
            className="rounded-md border border-control px-2 py-0.5 text-[11px] text-muted hover:border-control-hover hover:text-ink"
          >
            Clear
          </button>
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <div className="min-w-0 flex-1 overflow-y-auto">
          {visible.length === 0 ? (
            <div className="px-5 py-12 text-center text-[13px] text-muted">
              {search || facet
                ? "No matches — try clearing the filter."
                : "Queue clear. Pick another queue above, or search everywhere."}
            </div>
          ) : (
            <table className="w-full text-left text-[12.5px]">
              <thead className="sticky top-0 bg-surface">
                <tr className="border-b border-line text-[11px] text-faint">
                  <th className="w-8 px-3 py-1.5">
                    <input
                      type="checkbox"
                      checked={allSelected}
                      onChange={toggleSelectAll}
                      aria-label="Select all visible rows"
                      className="rounded border-control"
                    />
                  </th>
                  <th className="px-2 py-1.5">Shard</th>
                  <th className="w-20 px-2 py-1.5">Source</th>
                  <th className="w-16 px-2 py-1.5">Conf.</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((row, i) => (
                  <tr
                    key={row.shard.id}
                    onClick={() => setDetailId(row.shard.id)}
                    className={cn(
                      "cursor-pointer border-b border-line-2 transition-colors",
                      i === cursor && "bg-fg/3",
                      selected.has(row.shard.id) && "bg-fg/5",
                      "hover:bg-fg/5",
                    )}
                  >
                    <td className="px-3 py-1.5" onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        checked={selected.has(row.shard.id)}
                        onChange={() => toggleSelect(row.shard.id)}
                        aria-label={`Select ${row.shard.id}`}
                        className="rounded border-control"
                      />
                    </td>
                    <td className="max-w-md truncate px-2 py-1.5 text-ink" title={row.shard.text}>
                      {row.shard.text}
                    </td>
                    <td className="px-2 py-1.5 text-faint">{row.shard.origin || "—"}</td>
                    <td className="px-2 py-1.5 text-faint">
                      {row.score ? `${Math.round(row.score.confidence * 100)}%` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {filtered.length > visibleCount && (
            <div className="border-t border-line px-5 py-2 text-center">
              <button
                onClick={() => setVisibleCount((c) => c + PAGE_SIZE)}
                className="text-[12px] text-muted hover:text-ink"
              >
                Load {Math.min(PAGE_SIZE, filtered.length - visibleCount)} more
              </button>
            </div>
          )}
        </div>

        {detail && (
          <div className="w-[380px] flex-none overflow-y-auto border-l border-line bg-surface-2 p-4">
            <DetailPanel
              row={detail}
              clusters={clusters ?? []}
              publishedById={publishedById}
              judgeOn={judgeOn}
              onPublish={() => { review.publish.mutate(detail.shard.id); setDetailId(null); }}
              onReject={() => { review.reject.mutate(detail.shard.id); setDetailId(null); }}
              onUndo={() => { undoAuto.mutate(detail.shard.id); setDetailId(null); }}
              onAskJudge={() => judge.mutate(detail.shard.id)}
              onClose={() => setDetailId(null)}
              onPromoteCluster={(v) => promoteCluster.mutate(v)}
              promoteBusy={promoteCluster.isPending}
              judgePending={judge.isPending}
              busy={review.publish.isPending || review.reject.isPending}
            />
          </div>
        )}
      </div>

      {sweepOpen && (
        <SweepPreviewModal
          targets={sweepTargets}
          onConfirm={handleSweep}
          onCancel={() => setSweepOpen(false)}
          busy={review.reject.isPending}
        />
      )}

      {toast.visible && (
        <div className="fixed bottom-4 left-1/2 z-50 flex -translate-x-1/2 items-center gap-3 rounded-lg border border-line bg-surface px-4 py-2 shadow-lg">
          <span className="text-[12.5px] text-ink">{toast.message}</span>
          {toast.undoableIds.length > 0 && (
            <button
              onClick={() => handleUndo(toast.undoableIds)}
              className="inline-flex items-center gap-1 rounded-md border border-control px-2 py-0.5 text-[11px] text-muted hover:border-control-hover hover:text-ink"
            >
              <RotateCcw size={11} /> Undo
            </button>
          )}
          <button onClick={() => setToast((t) => ({ ...t, visible: false }))} className="text-faint hover:text-ink">
            <X size={14} />
          </button>
        </div>
      )}

      <div className="flex-none border-t border-line bg-surface px-5 py-1.5 text-[11px] text-faint">
        Memory policy: {active?.memory_write_mode ?? "review"} · auto-reject {active?.memory_auto_reject ? "on" : "off"}
      </div>
    </div>
  );
}

function DetailPanel({
  row,
  clusters,
  publishedById,
  judgeOn,
  onPublish,
  onReject,
  onUndo,
  onAskJudge,
  onClose,
  onPromoteCluster,
  promoteBusy,
  judgePending,
  busy,
}: {
  row: TriagedShard;
  clusters: ShardCluster[];
  publishedById: Map<string, Shard>;
  judgeOn: boolean;
  onPublish: () => void;
  onReject: () => void;
  onUndo: () => void;
  onAskJudge: () => void;
  onClose: () => void;
  onPromoteCluster: (v: { publishId: string; rejectIds: string[] }) => void;
  promoteBusy: boolean;
  judgePending: boolean;
  busy: boolean;
}) {
  const { shard, score } = row;
  const isAuto = Boolean(shard.scoring_source) && shard.status !== "candidate";

  const cluster = clusters.find(
    (c) => c.representative.id === shard.id || c.members.some((m) => m.id === shard.id),
  );

  const conflictRefs = (score?.conflicts ?? []).map((c) => {
    const match = c.match(/published\s+(\S+)/i);
    const id = match?.[1] ?? c;
    const pub = publishedById.get(id);
    return { id, text: pub?.text || c };
  });

  const [pickedCanonical, setPickedCanonical] = React.useState<string | null>(null);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <span className="text-[11px] text-faint">Detail</span>
        <button onClick={onClose} className="text-faint hover:text-ink"><X size={14} /></button>
      </div>

      <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-ink">{shard.text}</p>

      <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11.5px]">
        <span className="text-faint">Status</span>
        <span className="text-ink">{shard.status}</span>
        <span className="text-faint">Origin</span>
        <span className="text-ink">{shard.origin || "—"}</span>
        <span className="text-faint">Source</span>
        <span className="text-ink">{shard.source || "—"}</span>
        <span className="text-faint">Scope</span>
        <span className="text-ink">{shard.scope}</span>
        {shard.scoring_source && (
          <>
            <span className="text-faint">Scoring</span>
            <span className="text-ink">{shard.scoring_source}</span>
          </>
        )}
        {shard.auto_confidence != null && (
          <>
            <span className="text-faint">Confidence</span>
            <span className="text-ink">{Math.round(shard.auto_confidence * 100)}%</span>
          </>
        )}
        {shard.created_at && (
          <>
            <span className="text-faint">Created</span>
            <span className="text-ink">{new Date(shard.created_at).toLocaleDateString()}</span>
          </>
        )}
      </div>

      <div>
        <span className="mb-1 block text-[11px] text-faint">Class</span>
        <span className="text-[12px] text-ink">
          {shard.scope === "global" ? "Global" : shard.scope === "item" ? "Item-scoped" : shard.scope}
        </span>
      </div>

      {conflictRefs.length > 0 && (
        <div>
          <span className="mb-1 block text-[11px] text-faint">Contradicts</span>
          {conflictRefs.map((c) => (
            <p key={c.id} className="rounded border border-line-2 bg-surface p-2 text-[11.5px] text-ink">
              <a
                href="/memory-triage"
                className="font-mono text-[10.5px] text-st-blocked underline decoration-dotted underline-offset-2"
              >
                {c.id}
              </a>
              {c.text && c.text !== c.id ? (
                <span className="mt-1 block text-ink">{c.text}</span>
              ) : null}
            </p>
          ))}
        </div>
      )}

      {cluster && (
        <div>
          <span className="mb-1 block text-[11px] text-faint">Pick the canonical wording</span>
          <div className="flex flex-col gap-1">
            {[cluster.representative, ...cluster.members].map((m) => (
              <button
                key={m.id}
                onClick={() => setPickedCanonical(m.id)}
                className={cn(
                  "rounded border px-2 py-1.5 text-left text-[11.5px] transition-colors",
                  pickedCanonical === m.id
                    ? "border-fg bg-fg/5 text-ink"
                    : "border-control text-muted hover:border-control-hover hover:text-ink",
                )}
              >
                <span className="line-clamp-2">{m.text}</span>
              </button>
            ))}
          </div>
          {pickedCanonical && (
            <button
              onClick={() =>
                onPromoteCluster({
                  publishId: pickedCanonical,
                  rejectIds: [cluster.representative, ...cluster.members]
                    .filter((m) => m.id !== pickedCanonical)
                    .map((m) => m.id),
                })
              }
              disabled={promoteBusy}
              className="mt-2 inline-flex items-center gap-1 rounded-md border border-st-done/30 bg-st-done/10 px-2 py-1 text-[11.5px] text-st-done hover:bg-st-done/15 disabled:opacity-50"
            >
              <Check size={12} /> Publish as principle · drop {cluster.members.length}
            </button>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
        {shard.status === "candidate" && (
          <Button type="button" variant="outline" size="sm" onClick={onPublish} disabled={busy}>
            Publish
          </Button>
        )}
        {shard.status === "candidate" && (
          <Button type="button" variant="ghost" size="sm" onClick={onReject} disabled={busy}>
            Reject
          </Button>
        )}
        {isAuto && (
          <Button type="button" variant="outline" size="sm" onClick={onUndo} disabled={promoteBusy}>
            <RotateCcw size={12} className="mr-1" /> Return to queue
          </Button>
        )}
        {judgeOn && (
          <Button type="button" variant="ghost" size="sm" onClick={onAskJudge} disabled={judgePending}>
            {judgePending ? "Asking…" : "Ask the judge"}
          </Button>
        )}
      </div>
    </div>
  );
}

function SweepPreviewModal({
  targets,
  onConfirm,
  onCancel,
  busy,
}: {
  targets: TriagedShard[];
  onConfirm: () => void;
  onCancel: () => void;
  busy: boolean;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-fg/20">
      <div className="mx-4 w-full max-w-lg rounded-xl border border-line bg-surface p-5 shadow-lg">
        <h2 className="mb-1 text-[15px] font-semibold text-fg">Sweep low-confidence shards</h2>
        <p className="mb-3 text-[12.5px] text-muted">
          This will reject {targets.length} shard{targets.length === 1 ? "" : "s"} with confidence below {Math.round(SWEEP_THRESHOLD * 100)}%.
          You can undo each one from the toast.
        </p>
        <div className="mb-4 max-h-48 overflow-y-auto rounded-lg border border-line-2">
          {targets.map((t) => (
            <div key={t.shard.id} className="flex items-center gap-2 border-b border-line-2 px-3 py-1.5 last:border-b-0">
              <span className="text-[11px] text-faint">{Math.round((t.score?.confidence ?? 0) * 100)}%</span>
              <span className="flex-1 truncate text-[12px] text-ink">{t.shard.text}</span>
            </div>
          ))}
        </div>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>
          <Button type="button" variant="outline" size="sm" onClick={onConfirm} disabled={busy}>
            {busy ? "Sweeping…" : `Reject ${targets.length}`}
          </Button>
        </div>
      </div>
    </div>
  );
}
