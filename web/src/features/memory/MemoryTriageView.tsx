import {
  AlertTriangle,
  Check,
  ChevronDown,
  Copy,
  Layers,
  RotateCcw,
  Search,
  Sparkles,
  Trash2,
  X,
  Zap,
} from "lucide-react";
import * as React from "react";

import { MemoryReviewSkeleton, PlannerError } from "@/components/planner/PlannerStates";
import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { cn } from "@/lib/cn";
import { useProjectCtx } from "@/features/ProjectContext";
import {
  useAutoActions,
  useCandidateClusters,
  useCandidateShards,
  useCounts,
  useJudgeShard,
  useReviewShard,
  useScoredCandidates,
  useUndoAutoShard,
} from "@/lib/queries";
import type { ScoredCandidate, Shard, ShardCluster } from "@/lib/types";

type QueueKey = "needs" | "conflicts" | "dupes" | "stale" | "unvetted";

interface QueueDef {
  key: QueueKey;
  label: string;
  hint: string;
}

const QUEUES: QueueDef[] = [
  { key: "needs", label: "Needs review", hint: "Scorer could not decide" },
  { key: "conflicts", label: "Conflicts", hint: "Contradicts a published shard" },
  { key: "dupes", label: "Duplicates", hint: "Near-duplicate of another candidate" },
  { key: "stale", label: "Auto-acted", hint: "Published or rejected without a human" },
  { key: "unvetted", label: "Unvetted", hint: "Auto-published from a trusted/agent source — nobody looked" },
];

const UNVETTED_SOURCES = ["trusted", "agent"];
const PAGE_SIZE = 50;

type SortKey = "newest" | "oldest" | "confidence";

interface TriageRow {
  shard: Shard;
  score?: ScoredCandidate;
  queue: QueueKey;
  cluster?: ShardCluster;
}

export function MemoryTriageView() {
  const { activeId, active } = useProjectCtx();
  const judgeOn = Boolean(active?.memory_llm_judge);
  const { data: counts } = useCounts(activeId);
  const {
    data: candidates,
    isPending,
    isFetching,
    isError,
    isSuccess,
    refetch,
    error,
  } = useCandidateShards(activeId);
  const { data: clusters } = useCandidateClusters(activeId);
  const { data: scored } = useScoredCandidates(activeId);
  const { data: autoActions } = useAutoActions(activeId);
  const { publish, reject } = useReviewShard();
  const undoAuto = useUndoAutoShard();
  const judge = useJudgeShard();

  const [queue, setQueue] = React.useState<QueueKey>("needs");
  const [search, setSearch] = React.useState("");
  const [sort, setSort] = React.useState<SortKey>("newest");
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [focusIdx, setFocusIdx] = React.useState(0);
  const [visibleCount, setVisibleCount] = React.useState(PAGE_SIZE);
  const [detailId, setDetailId] = React.useState<string | null>(null);
  const [sweepOpen, setSweepOpen] = React.useState(false);
  const [undoToast, setUndoToast] = React.useState<{ id: string; label: string } | null>(null);
  const undoTimer = React.useRef<ReturnType<typeof setTimeout>>(undefined);

  const awaitingFirstPayload = isPending || (isFetching && candidates === undefined);
  const loadFailed = isError || (isSuccess && candidates === undefined);
  const loadErrorMessage =
    error instanceof Error && error.message.includes("timed out")
      ? "Memory triage is taking too long to load. Retry, or check the server."
      : "Memory triage unavailable";

  // Build the unified row list from candidates + auto-actions.
  const rows = React.useMemo<TriageRow[]>(() => {
    if (!candidates) return [];
    const scoreById = new Map((scored ?? []).map((s) => [s.shard.id, s]));
    const clusterByShard = new Map<string, ShardCluster>();
    for (const c of clusters ?? []) {
      clusterByShard.set(c.representative.id, c);
      for (const m of c.members) clusterByShard.set(m.id, c);
    }

    const result: TriageRow[] = [];

    for (const shard of candidates) {
      const score = scoreById.get(shard.id);
      const cluster = clusterByShard.get(shard.id);
      let q: QueueKey;
      if ((score?.conflicts?.length ?? 0) > 0) q = "conflicts";
      else if (score?.duplicate_of) q = "dupes";
      else if (score?.suggestion === "review") q = "needs";
      else q = "needs";
      result.push({ shard, score, queue: q, cluster });
    }

    for (const shard of autoActions ?? []) {
      const q: QueueKey = UNVETTED_SOURCES.includes(shard.scoring_source) ? "unvetted" : "stale";
      result.push({ shard, queue: q });
    }

    return result;
  }, [candidates, scored, clusters, autoActions]);

  const queueCounts = React.useMemo(() => {
    const m: Record<QueueKey, number> = { needs: 0, conflicts: 0, dupes: 0, stale: 0, unvetted: 0 };
    for (const r of rows) m[r.queue]++;
    return m;
  }, [rows]);

  const filtered = React.useMemo(() => {
    let list = rows.filter((r) => r.queue === queue);
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter((r) => r.shard.text.toLowerCase().includes(q) || r.shard.origin?.toLowerCase().includes(q));
    }
    list.sort((a, b) => {
      if (sort === "newest") return b.shard.created_at.localeCompare(a.shard.created_at);
      if (sort === "oldest") return a.shard.created_at.localeCompare(b.shard.created_at);
      return (b.score?.confidence ?? 0) - (a.score?.confidence ?? 0);
    });
    return list;
  }, [rows, queue, search, sort]);

  const visible = filtered.slice(0, visibleCount);
  const hasMore = visibleCount < filtered.length;

  // Reset page and selection on queue change.
  React.useEffect(() => {
    setVisibleCount(PAGE_SIZE);
    setSelected(new Set());
    setFocusIdx(0);
  }, [queue]);

  // Undo toast auto-dismiss.
  React.useEffect(() => {
    if (!undoToast) return;
    undoTimer.current = setTimeout(() => setUndoToast(null), 5000);
    return () => clearTimeout(undoTimer.current);
  }, [undoToast]);

  // Keyboard nav: J/K move, X select, Enter opens detail.
  React.useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (sweepOpen) return;
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
      if (e.key === "j" || e.key === "J") {
        e.preventDefault();
        setFocusIdx((i) => Math.min(i + 1, visible.length - 1));
      } else if (e.key === "k" || e.key === "K") {
        e.preventDefault();
        setFocusIdx((i) => Math.max(i - 1, 0));
      } else if (e.key === "x" || e.key === "X") {
        e.preventDefault();
        const row = visible[focusIdx];
        if (!row) return;
        setSelected((prev) => {
          const next = new Set(prev);
          if (next.has(row.shard.id)) next.delete(row.shard.id);
          else next.add(row.shard.id);
          return next;
        });
      } else if (e.key === "Enter") {
        e.preventDefault();
        const row = visible[focusIdx];
        if (row) setDetailId(row.shard.id);
      } else if (e.key === "Escape") {
        setDetailId(null);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [visible, focusIdx, sweepOpen]);

  const detailRow = detailId ? rows.find((r) => r.shard.id === detailId) : null;

  const toggleSelect = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const handlePublish = (id: string) => {
    publish.mutate(id, {
      onSuccess: () => {
        setUndoToast({ id, label: "Published" });
        if (detailId === id) setDetailId(null);
      },
    });
  };

  const handleReject = (id: string) => {
    reject.mutate(id, {
      onSuccess: () => {
        setUndoToast({ id, label: "Rejected" });
        if (detailId === id) setDetailId(null);
      },
    });
  };

  const handleUndo = (id: string) => {
    undoAuto.mutate(id, {
      onSuccess: () => setUndoToast({ id, label: "Returned to queue" }),
    });
  };

  const handleBatchPublish = () => {
    for (const id of selected) publish.mutate(id);
    setUndoToast({ id: "batch", label: `Published ${selected.size}` });
    setSelected(new Set());
  };

  const handleBatchReject = () => {
    for (const id of selected) reject.mutate(id);
    setUndoToast({ id: "batch", label: `Rejected ${selected.size}` });
    setSelected(new Set());
  };

  // Sweep: reject all candidates below 30% confidence.
  const sweepTargets = React.useMemo(
    () =>
      rows.filter(
        (r) => r.queue === "needs" && r.score && r.score.confidence < 0.3,
      ),
    [rows],
  );

  const handleSweep = () => {
    for (const t of sweepTargets) reject.mutate(t.shard.id);
    setUndoToast({ id: "sweep", label: `Swept ${sweepTargets.length} low-confidence` });
    setSweepOpen(false);
  };

  const header = (
    <PlaceHeader
      viewName="Memory triage"
      purpose="Operational view across the memory pipeline. Five queues separate what needs attention from what was auto-acted — sweep, bulk-publish, or drill into a shard."
      action={
        <div className="flex items-center gap-3 font-mono text-[10.5px] text-faint">
          {awaitingFirstPayload ? (
            <span className="text-muted">loading queue…</span>
          ) : (
            <span>{rows.length} TOTAL</span>
          )}
          {counts?.review != null && (
            <span className="text-st-review">{counts.review} in nav count</span>
          )}
        </div>
      }
    />
  );

  if (awaitingFirstPayload) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        {header}
        <div className="min-h-0 flex-1 overflow-y-auto">
          <MemoryReviewSkeleton />
        </div>
      </div>
    );
  }

  if (loadFailed || !candidates) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        {header}
        <PlannerError message={loadErrorMessage} onRetry={() => refetch()} />
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header strip */}
      <div className="flex flex-none items-center gap-4 border-b border-line px-5 py-4">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight">Memory triage</h1>
          <p className="mt-0.5 text-[12.5px] text-muted">
            Five queues across the memory pipeline — what needs a human, what conflicts, what is a duplicate,
            and what was auto-acted without review.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-3 font-mono text-[10.5px] text-faint">
          <span>{rows.length} TOTAL</span>
          {counts?.review != null && counts.review > 0 && (
            <span className="text-st-review">{counts.review} in nav count</span>
          )}
        </div>
      </div>

      {/* Queue facet chips */}
      <div className="flex flex-none items-center gap-2 border-b border-line px-5 py-2.5">
        {QUEUES.map((q) => (
          <button
            key={q.key}
            onClick={() => setQueue(q.key)}
            title={q.hint}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[12px] transition-colors",
              queue === q.key
                ? "border-accent/40 bg-accent/[0.08] text-ink"
                : "border-line-2 text-muted hover:border-line-hover hover:text-fg-2",
            )}
          >
            {q.label}
            <span className="rounded bg-surface-4 px-1 py-px font-mono text-[10px] text-muted">
              {queueCounts[q.key]}
            </span>
          </button>
        ))}

        <div className="ml-auto flex items-center gap-2">
          {/* Search */}
          <div className="relative">
            <Search size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-faint" />
            <input
              type="text"
              placeholder="Search shards…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-8 w-[180px] rounded-lg border border-line-2 bg-surface-2 pl-7 pr-2 text-[12px] text-ink placeholder:text-faint focus:border-accent/40 focus:outline-none"
            />
          </div>

          {/* Sort */}
          <div className="relative">
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value as SortKey)}
              className="h-8 appearance-none rounded-lg border border-line-2 bg-surface-2 pl-2.5 pr-7 text-[12px] text-muted focus:border-accent/40 focus:outline-none"
            >
              <option value="newest">Newest</option>
              <option value="oldest">Oldest</option>
              <option value="confidence">Confidence</option>
            </select>
            <ChevronDown size={12} className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-faint" />
          </div>

          {/* Sweep button */}
          {sweepTargets.length > 0 && (
            <button
              onClick={() => setSweepOpen(true)}
              className="inline-flex items-center gap-1.5 rounded-lg border border-[rgba(255,107,107,0.3)] bg-[rgba(255,107,107,0.06)] px-2.5 py-1.5 text-[12px] text-st-blocked transition-colors hover:bg-[rgba(255,107,107,0.12)]"
            >
              <Zap size={12} />
              Sweep {sweepTargets.length} low-confidence
            </button>
          )}
        </div>
      </div>

      {/* Bulk action bar */}
      {selected.size > 0 && (
        <div className="flex flex-none items-center gap-3 border-b border-line bg-surface-2/80 px-5 py-2">
          <span className="font-mono text-[11px] text-muted">{selected.size} selected</span>
          <button
            onClick={handleBatchPublish}
            disabled={publish.isPending}
            className="inline-flex items-center gap-1 rounded-md border border-[#1c2620] bg-[rgba(95,208,122,0.08)] px-2 py-1 text-[11.5px] text-st-done transition-colors hover:bg-[rgba(95,208,122,0.14)] disabled:opacity-50"
          >
            <Check size={12} /> Publish all
          </button>
          <button
            onClick={handleBatchReject}
            disabled={reject.isPending}
            className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11.5px] text-muted transition-colors hover:border-line-hover hover:text-ink disabled:opacity-50"
          >
            <X size={12} /> Reject all
          </button>
          <button
            onClick={() => setSelected(new Set())}
            className="ml-auto text-[11px] text-faint hover:text-muted"
          >
            Clear selection
          </button>
        </div>
      )}

      {/* Main content: row table + detail panel */}
      <div className="flex min-h-0 flex-1">
        {/* Row table */}
        <div className="min-h-0 flex-1 overflow-y-auto">
          {filtered.length === 0 ? (
            <div className="py-16 text-center text-[13px] text-muted">
              {search ? "No shards match your search." : `Nothing in the ${QUEUES.find((q) => q.key === queue)?.label ?? ""} queue.`}
            </div>
          ) : (
            <div className="flex flex-col">
              {visible.map((row, idx) => (
                <TriageRowItem
                  key={row.shard.id}
                  row={row}
                  focused={idx === focusIdx}
                  isSelected={selected.has(row.shard.id)}
                  onToggleSelect={() => toggleSelect(row.shard.id)}
                  onOpenDetail={() => setDetailId(row.shard.id)}
                />
              ))}
              {hasMore && (
                <button
                  onClick={() => setVisibleCount((c) => c + PAGE_SIZE)}
                  className="border-t border-line px-5 py-3 text-center text-[12px] text-muted transition-colors hover:bg-surface-2 hover:text-fg-2"
                >
                  Load {Math.min(PAGE_SIZE, filtered.length - visibleCount)} more
                </button>
              )}
            </div>
          )}
        </div>

        {/* Detail panel */}
        {detailRow && (
          <div className="flex w-[380px] flex-none flex-col border-l border-line overflow-y-auto">
            <DetailPanel
              row={detailRow}
              judgeOn={judgeOn}
              busy={publish.isPending || reject.isPending}
              onPublish={() => handlePublish(detailRow.shard.id)}
              onReject={() => handleReject(detailRow.shard.id)}
              onAskJudge={() => judge.mutate(detailRow.shard.id)}
              onUndo={() => handleUndo(detailRow.shard.id)}
              onClose={() => setDetailId(null)}
            />
          </div>
        )}
      </div>

      {/* Policy strip */}
      <div className="flex flex-none items-center gap-2 border-t border-line bg-surface-2/40 px-5 py-2">
        <span className="font-mono text-[10px] uppercase tracking-wide text-faint">
          J/K navigate · X select · Enter detail · Esc close
        </span>
        <span className="ml-auto font-mono text-[10px] text-faint">
          Only published shards surface in search — candidates are invisible until reviewed.
        </span>
      </div>

      {/* Sweep preview modal */}
      {sweepOpen && (
        <SweepPreviewModal
          targets={sweepTargets}
          onConfirm={handleSweep}
          onCancel={() => setSweepOpen(false)}
          busy={reject.isPending}
        />
      )}

      {/* Undo toast */}
      {undoToast && (
        <div className="fixed bottom-6 left-1/2 z-50 flex -translate-x-1/2 items-center gap-3 rounded-xl border border-line-2 bg-surface-2 px-4 py-2.5 shadow-lg">
          <span className="text-[13px] text-ink">{undoToast.label}</span>
          <button
            onClick={() => {
              if (undoToast.id !== "batch" && undoToast.id !== "sweep") {
                // Undo the last action by returning to queue.
                const row = rows.find((r) => r.shard.id === undoToast.id);
                if (row && UNVETTED_SOURCES.includes(row.shard.scoring_source)) {
                  handleUndo(row.shard.id);
                }
              }
              setUndoToast(null);
            }}
            className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11.5px] text-muted transition-colors hover:border-line-hover hover:text-ink"
          >
            <RotateCcw size={11} /> Undo
          </button>
          <button onClick={() => setUndoToast(null)} className="text-faint hover:text-muted">
            <X size={14} />
          </button>
        </div>
      )}
    </div>
  );
}

function TriageRowItem({
  row,
  focused,
  isSelected,
  onToggleSelect,
  onOpenDetail,
}: {
  row: TriageRow;
  focused: boolean;
  isSelected: boolean;
  onToggleSelect: () => void;
  onOpenDetail: () => void;
}) {
  const { shard, score, queue } = row;
  return (
    <div
      onClick={onOpenDetail}
      className={cn(
        "flex cursor-pointer items-center gap-3 border-b border-line/50 px-5 py-2.5 transition-colors",
        focused && "bg-surface-3/60",
        isSelected && "bg-accent/[0.04]",
      )}
    >
      <button
        onClick={(e) => {
          e.stopPropagation();
          onToggleSelect();
        }}
        className={cn(
          "flex h-4 w-4 flex-none items-center justify-center rounded border transition-colors",
          isSelected
            ? "border-accent bg-accent/20"
            : "border-line-2 hover:border-line-hover",
        )}
      >
        {isSelected && <Check size={10} className="text-accent" />}
      </button>

      <div className="min-w-0 flex-1">
        <p className="truncate text-[13px] text-ink">{shard.text}</p>
        <div className="mt-0.5 flex items-center gap-2 text-[11px] text-faint">
          <span>{shard.origin || "agent"}</span>
          {shard.source && <span>· {shard.source}</span>}
          {score?.confidence != null && (
            <span className="font-mono">{Math.round(score.confidence * 100)}%</span>
          )}
        </div>
      </div>

      <div className="flex items-center gap-1.5">
        {queue === "conflicts" && (
          <AlertTriangle size={12} className="text-st-blocked" />
        )}
        {queue === "dupes" && (
          <Copy size={12} className="text-[#e0b34a]" />
        )}
        {(queue === "stale" || queue === "unvetted") && (
          <Sparkles size={12} className={queue === "unvetted" ? "text-[#a78bfa]" : "text-faint"} />
        )}
        <QueueBadge queue={queue} />
      </div>
    </div>
  );
}

function QueueBadge({ queue }: { queue: QueueKey }) {
  const meta: Record<QueueKey, { label: string; cls: string }> = {
    needs: { label: "review", cls: "border-[#3a2f1a] bg-[rgba(224,179,74,0.08)] text-[#e0b34a]" },
    conflicts: { label: "conflict", cls: "border-[rgba(255,107,107,0.3)] bg-[rgba(255,107,107,0.08)] text-st-blocked" },
    dupes: { label: "dupe", cls: "border-line-2 bg-surface-3 text-muted" },
    stale: { label: "auto", cls: "border-line-2 bg-surface-3 text-faint" },
    unvetted: { label: "unvetted", cls: "border-[rgba(167,139,250,0.35)] bg-[rgba(167,139,250,0.1)] text-[#a78bfa]" },
  };
  const m = meta[queue];
  return (
    <span className={cn("rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide", m.cls)}>
      {m.label}
    </span>
  );
}

function DetailPanel({
  row,
  judgeOn,
  busy,
  onPublish,
  onReject,
  onAskJudge,
  onUndo,
  onClose,
}: {
  row: TriageRow;
  judgeOn: boolean;
  busy: boolean;
  onPublish: () => void;
  onReject: () => void;
  onAskJudge: () => void;
  onUndo: () => void;
  onClose: () => void;
}) {
  const { shard, score, cluster } = row;
  const isAuto = row.queue === "stale" || row.queue === "unvetted";
  const contradictedId = score?.conflicts?.[0] ?? null;

  return (
    <div className="flex flex-col">
      {/* Close bar */}
      <div className="flex items-center justify-between border-b border-line px-4 py-3">
        <span className="font-mono text-[10.5px] uppercase tracking-wide text-faint">Shard detail</span>
        <button onClick={onClose} className="text-faint hover:text-muted">
          <X size={14} />
        </button>
      </div>

      {/* Shard text */}
      <div className="border-b border-line px-4 py-3">
        <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-ink">{shard.text}</p>
      </div>

      {/* Metadata grid */}
      <div className="grid grid-cols-2 gap-x-4 gap-y-2 border-b border-line px-4 py-3 text-[12px]">
        <MetaRow label="Origin" value={shard.origin || "agent"} />
        <MetaRow label="Source" value={shard.source || "—"} />
        <MetaRow label="Scope" value={shard.scope || "—"} />
        <MetaRow label="Status" value={shard.status} />
        <MetaRow label="Created" value={new Date(shard.created_at).toLocaleDateString()} />
        {score?.confidence != null && (
          <MetaRow label="Confidence" value={`${Math.round(score.confidence * 100)}%`} />
        )}
        {score?.suggestion && <MetaRow label="Suggestion" value={score.suggestion} />}
        {shard.scoring_source && <MetaRow label="Scoring" value={shard.scoring_source} />}
      </div>

      {/* Contradicted shard */}
      {contradictedId && (
        <div className="border-b border-line px-4 py-3">
          <span className="font-mono text-[10px] uppercase tracking-wide text-st-blocked">Contradicts</span>
          <p className="mt-1 text-[12px] text-muted">Published shard {contradictedId}</p>
        </div>
      )}

      {/* Canonical wording (cluster near-duplicates) */}
      {cluster && cluster.members.length > 0 && (
        <div className="border-b border-line px-4 py-3">
          <div className="mb-1.5 flex items-center gap-1.5">
            <Layers size={12} className="text-[#e0b34a]" />
            <span className="font-mono text-[10px] uppercase tracking-wide text-[#e0b34a]">
              Near-duplicates · {cluster.size}×
            </span>
          </div>
          <p className="mb-1.5 text-[12px] text-fg-2">{cluster.representative.text}</p>
          {cluster.members.slice(0, 3).map((m) => (
            <p key={m.id} className="mt-1 truncate text-[11.5px] text-faint" title={m.text}>
              {m.text}
            </p>
          ))}
        </div>
      )}

      {/* Score reasons */}
      {score && score.reasons.length > 0 && (
        <div className="border-b border-line px-4 py-3">
          <span className="font-mono text-[10px] uppercase tracking-wide text-faint">Reasons</span>
          <p className="mt-1 text-[12px] text-muted">{score.reasons.join(" · ")}</p>
        </div>
      )}

      {/* Actions */}
      <div className="flex flex-wrap items-center gap-2 px-4 py-3">
        {isAuto ? (
          <button
            onClick={onUndo}
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-[12px] text-muted transition-colors hover:border-line-hover hover:text-ink disabled:opacity-50"
          >
            <RotateCcw size={13} /> Return to queue
          </button>
        ) : (
          <>
            <button
              onClick={onPublish}
              disabled={busy}
              className="inline-flex items-center gap-1.5 rounded-lg border border-[#1c2620] bg-[rgba(95,208,122,0.08)] px-2.5 py-1.5 text-[12px] font-medium text-st-done transition-colors hover:bg-[rgba(95,208,122,0.14)] disabled:opacity-50"
            >
              <Check size={13} /> Publish
            </button>
            <button
              onClick={onReject}
              disabled={busy}
              className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-[12px] text-muted transition-colors hover:border-line-hover hover:text-ink disabled:opacity-50"
            >
              <X size={13} /> Reject
            </button>
            {judgeOn && (
              <button
                onClick={onAskJudge}
                disabled={busy}
                className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-[12px] text-muted transition-colors hover:border-line-hover hover:text-ink disabled:opacity-50"
              >
                Ask the judge
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <span className="font-mono text-[10px] uppercase tracking-wide text-faint">{label}</span>
      <span className="text-fg-2">{value}</span>
    </>
  );
}

function SweepPreviewModal({
  targets,
  onConfirm,
  onCancel,
  busy,
}: {
  targets: TriageRow[];
  onConfirm: () => void;
  onCancel: () => void;
  busy: boolean;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="w-[480px] rounded-2xl border border-line-2 bg-surface-2 p-5 shadow-xl">
        <div className="mb-3 flex items-center gap-2">
          <Zap size={16} className="text-[#e0b34a]" />
          <h2 className="text-[15px] font-semibold">Sweep low-confidence candidates</h2>
        </div>
        <p className="mb-3 text-[12.5px] text-muted">
          This will reject {targets.length} candidate{targets.length === 1 ? "" : "s"} below 30% confidence.
          Review the list before confirming.
        </p>
        <div className="mb-4 max-h-[240px] overflow-y-auto rounded-lg border border-line bg-surface-3/50">
          {targets.map((t) => (
            <div key={t.shard.id} className="flex items-center gap-2 border-b border-line/50 px-3 py-2 last:border-b-0">
              <span className="font-mono text-[10px] text-faint">
                {t.score ? Math.round(t.score.confidence * 100) : 0}%
              </span>
              <p className="min-w-0 flex-1 truncate text-[12px] text-fg-2">{t.shard.text}</p>
            </div>
          ))}
        </div>
        <div className="flex items-center justify-end gap-2">
          <button
            onClick={onCancel}
            className="rounded-lg border border-line px-3 py-1.5 text-[12px] text-muted transition-colors hover:border-line-hover hover:text-ink"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded-lg border border-[rgba(255,107,107,0.3)] bg-[rgba(255,107,107,0.08)] px-3 py-1.5 text-[12px] font-medium text-st-blocked transition-colors hover:bg-[rgba(255,107,107,0.14)] disabled:opacity-50"
          >
            <Trash2 size={13} /> Reject {targets.length}
          </button>
        </div>
      </div>
    </div>
  );
}
