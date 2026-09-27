import {
  AlertTriangle,
  ArrowDownUp,
  Check,
  ChevronDown,
  Layers,
  Loader2,
  RotateCcw,
  Search,
  Sparkles,
  X,
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
  useJudgeShard,
  useReviewShard,
  useScoredCandidates,
  useUndoAutoShard,
} from "@/lib/queries";
import type { CandidateJudge, ScoredCandidate, Shard, ShardCluster } from "@/lib/types";
import { UNVETTED_SOURCES } from "./MemoryReviewView";

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
  { key: "stale", label: "Stale", hint: "No longer fresh" },
  { key: "unvetted", label: "Unvetted", hint: "Auto-published without human review" },
];

type SortKey = "newest" | "oldest" | "confidence";

interface UndoEntry {
  shardId: string;
  text: string;
  action: "published" | "rejected";
}

export function MemoryTriageView() {
  const { activeId, active } = useProjectCtx();
  const judgeOn = Boolean(active?.memory_llm_judge);

  const { data: candidates, isPending, isError, error, refetch } = useCandidateShards(activeId);
  const { data: scored } = useScoredCandidates(activeId);
  const { data: clusters } = useCandidateClusters(activeId);
  const { data: autoActions } = useAutoActions(activeId);

  const [activeQueue, setActiveQueue] = React.useState<QueueKey>("needs");
  const [search, setSearch] = React.useState("");
  const [sort, setSort] = React.useState<SortKey>("newest");
  const [facet, setFacet] = React.useState<string | null>(null);
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [cursor, setCursor] = React.useState(0);
  const [visibleCount, setVisibleCount] = React.useState(50);
  const [detailId, setDetailId] = React.useState<string | null>(null);
  const [undo, setUndo] = React.useState<UndoEntry | null>(null);
  const [sweepPreview, setSweepPreview] = React.useState(false);

  const { publish, reject } = useReviewShard();
  const undoAuto = useUndoAutoShard();
  const judge = useJudgeShard();

  const awaiting = isPending && candidates === undefined;

  const scoreMap = React.useMemo(
    () => new Map((scored ?? []).map((s) => [s.shard.id, s])),
    [scored],
  );
  const clusterById = React.useMemo(() => {
    const m = new Map<string, ShardCluster>();
    for (const c of clusters ?? []) {
      m.set(c.representative.id, c);
      for (const mem of c.members) m.set(mem.id, c);
    }
    return m;
  }, [clusters]);

  const queueCounts = React.useMemo(() => {
    const counts: Record<QueueKey, number> = { needs: 0, conflicts: 0, dupes: 0, stale: 0, unvetted: 0 };
    for (const sc of scored ?? []) {
      if (sc.suggestion === "review") counts.needs++;
      if ((sc.conflicts?.length ?? 0) > 0) counts.conflicts++;
      if (sc.duplicate_of) counts.dupes++;
      if (!sc.shard.fresh) counts.stale++;
    }
    for (const s of autoActions ?? []) {
      if (UNVETTED_SOURCES.includes(s.scoring_source)) counts.unvetted++;
    }
    return counts;
  }, [scored, autoActions]);

  const queueRows = React.useMemo(() => {
    let rows: { shard: Shard; score?: ScoredCandidate }[] = [];
    switch (activeQueue) {
      case "needs":
        rows = (scored ?? [])
          .filter((s) => s.suggestion === "review")
          .map((s) => ({ shard: s.shard, score: s }));
        break;
      case "conflicts":
        rows = (scored ?? [])
          .filter((s) => (s.conflicts?.length ?? 0) > 0)
          .map((s) => ({ shard: s.shard, score: s }));
        break;
      case "dupes":
        rows = (scored ?? [])
          .filter((s) => s.duplicate_of != null)
          .map((s) => ({ shard: s.shard, score: s }));
        break;
      case "stale":
        rows = (scored ?? [])
          .filter((s) => !s.shard.fresh)
          .map((s) => ({ shard: s.shard, score: s }));
        break;
      case "unvetted":
        rows = (autoActions ?? [])
          .filter((s) => UNVETTED_SOURCES.includes(s.scoring_source))
          .map((s) => ({ shard: s, score: scoreMap.get(s.id) }));
        break;
    }
    if (search.trim()) {
      const q = search.toLowerCase();
      rows = rows.filter((r) => r.shard.text.toLowerCase().includes(q) || r.shard.origin.toLowerCase().includes(q));
    }
    if (facet) {
      rows = rows.filter((r) => r.shard.source === facet || r.shard.origin === facet);
    }
    rows.sort((a, b) => {
      switch (sort) {
        case "oldest":
          return a.shard.created_at.localeCompare(b.shard.created_at);
        case "confidence":
          return (b.score?.confidence ?? 0) - (a.score?.confidence ?? 0);
        default:
          return b.shard.created_at.localeCompare(a.shard.created_at);
      }
    });
    return rows;
  }, [activeQueue, scored, autoActions, search, facet, sort, scoreMap]);

  const visibleRows = queueRows.slice(0, visibleCount);
  const hasMore = visibleCount < queueRows.length;
  const facets = React.useMemo(() => {
    const sources = new Set<string>();
    for (const r of queueRows) {
      if (r.shard.source) sources.add(r.shard.source);
      if (r.shard.origin) sources.add(r.shard.origin);
    }
    return [...sources].sort();
  }, [queueRows]);

  const detail = detailId ? queueRows.find((r) => r.shard.id === detailId) ?? null : null;

  const toggleSelect = React.useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const handlePublish = React.useCallback((id: string, text: string) => {
    publish.mutate(id, {
      onSuccess: () => {
        setUndo({ shardId: id, text, action: "published" });
        setTimeout(() => setUndo((u) => (u?.shardId === id ? null : u)), 5000);
      },
    });
  }, [publish]);

  const handleReject = React.useCallback((id: string, text: string) => {
    reject.mutate(id, {
      onSuccess: () => {
        setUndo({ shardId: id, text, action: "rejected" });
        setTimeout(() => setUndo((u) => (u?.shardId === id ? null : u)), 5000);
      },
    });
  }, [reject]);

  const handleUndo = React.useCallback(() => {
    if (!undo) return;
    if (undo.action === "published") {
      reject.mutate(undo.shardId);
    } else {
      publish.mutate(undo.shardId);
    }
    setUndo(null);
  }, [undo, publish, reject]);

  const handleBatchPublish = React.useCallback(() => {
    for (const id of selected) {
      const row = queueRows.find((r) => r.shard.id === id);
      if (row) publish.mutate(id);
    }
    setSelected(new Set());
  }, [selected, queueRows, publish]);

  const handleBatchReject = React.useCallback(() => {
    for (const id of selected) {
      const row = queueRows.find((r) => r.shard.id === id);
      if (row) reject.mutate(id);
    }
    setSelected(new Set());
  }, [selected, queueRows, reject]);

  const handleSweep = React.useCallback(() => {
    const toReject = queueRows.filter(
      (r) => r.score && (r.score.suggestion === "reject" || r.score.confidence < 0.3),
    );
    for (const r of toReject) reject.mutate(r.shard.id);
    setSweepPreview(false);
  }, [queueRows, reject]);

  React.useEffect(() => {
    setVisibleCount(50);
    setCursor(0);
    setSelected(new Set());
  }, [activeQueue, search, sort, facet]);

  const handleKeyDown = React.useCallback(
    (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      switch (e.key) {
        case "j":
          setCursor((c) => Math.min(c + 1, visibleRows.length - 1));
          break;
        case "k":
          setCursor((c) => Math.max(c - 1, 0));
          break;
        case "x":
        case "X":
          if (visibleRows[cursor]) toggleSelect(visibleRows[cursor].shard.id);
          break;
        case "Enter":
          if (visibleRows[cursor]) setDetailId(visibleRows[cursor].shard.id);
          break;
        case "Escape":
          setDetailId(null);
          break;
      }
    },
    [cursor, visibleRows, toggleSelect],
  );

  React.useEffect(() => {
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  const header = (
    <PlaceHeader
      viewName="Memory triage"
      purpose="Five queues for structured triage — needs review, conflicts, duplicates, stale, and unvetted auto-actions. Search, sort, facet, bulk-select, and sweep."
      action={
        <div className="flex items-center gap-3 font-mono text-[10.5px] text-faint">
          <span>{(scored ?? []).length} CANDIDATES</span>
          {queueCounts.unvetted > 0 && (
            <span className="text-[#a78bfa]">{queueCounts.unvetted} UNVETTED</span>
          )}
        </div>
      }
    />
  );

  if (awaiting) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        {header}
        <div className="min-h-0 flex-1 overflow-y-auto">
          <MemoryReviewSkeleton />
        </div>
      </div>
    );
  }

  if (isError && !candidates) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        {header}
        <PlannerError
          message={error instanceof Error ? error.message : "Memory triage unavailable"}
          onRetry={() => refetch()}
        />
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {header}

      {/* Policy strip */}
      <div className="flex-none border-b border-line bg-surface-2/50 px-5 py-2">
        <p className="text-[11.5px] text-muted">
          Only published shards enter the retrieval path. Rejecting keeps the shard for provenance but excludes it from search.
          Auto-published shards (trusted/agent) need human review to enter the corroboration pool.
        </p>
      </div>

      {/* Queue tabs */}
      <div className="flex-none border-b border-line px-5">
        <div className="flex items-center gap-1 overflow-x-auto py-2">
          {QUEUES.map((q) => (
            <button
              key={q.key}
              onClick={() => setActiveQueue(q.key)}
              title={q.hint}
              className={cn(
                "flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[12px] font-medium transition-colors",
                activeQueue === q.key
                  ? "bg-surface-3 text-ink"
                  : "text-muted hover:bg-surface-2 hover:text-ink",
              )}
            >
              {q.label}
              <span className={cn(
                "rounded-full px-1.5 py-0.5 font-mono text-[10px]",
                activeQueue === q.key ? "bg-surface-4 text-ink" : "bg-surface-2 text-faint",
              )}>
                {queueCounts[q.key]}
              </span>
            </button>
          ))}
        </div>
      </div>

      {/* Toolbar: search, sort, facets */}
      <div className="flex-none border-b border-line px-5 py-2.5">
        <div className="flex items-center gap-3">
          <div className="relative flex-1 max-w-xs">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search shards…"
              className="w-full rounded-lg border border-line bg-surface-2 py-1.5 pl-8 pr-3 text-[12.5px] text-ink placeholder:text-faint focus:border-line-hover focus:outline-none"
            />
          </div>
          <div className="relative">
            <ArrowDownUp size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value as SortKey)}
              className="appearance-none rounded-lg border border-line bg-surface-2 py-1.5 pl-7 pr-7 text-[12px] text-muted focus:border-line-hover focus:outline-none"
            >
              <option value="newest">Newest</option>
              <option value="oldest">Oldest</option>
              <option value="confidence">Confidence</option>
            </select>
            <ChevronDown size={12} className="absolute right-2 top-1/2 -translate-y-1/2 text-faint pointer-events-none" />
          </div>
          {facets.length > 0 && (
            <div className="flex items-center gap-1">
              {facets.slice(0, 5).map((f) => (
                <button
                  key={f}
                  onClick={() => setFacet(facet === f ? null : f)}
                  className={cn(
                    "rounded-md border px-2 py-1 text-[10.5px] transition-colors",
                    facet === f
                      ? "border-line-hover bg-surface-3 text-ink"
                      : "border-line text-faint hover:border-line-hover hover:text-muted",
                  )}
                >
                  {f}
                </button>
              ))}
            </div>
          )}
          <div className="ml-auto flex items-center gap-2">
            {queueRows.length > 0 && (
              <button
                onClick={() => setSweepPreview(true)}
                className="rounded-lg border border-[rgba(255,107,107,0.3)] bg-[rgba(255,107,107,0.06)] px-3 py-1.5 text-[11.5px] text-st-blocked transition-colors hover:bg-[rgba(255,107,107,0.12)]"
              >
                Sweep low-confidence
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Bulk action bar */}
      {selected.size > 0 && (
        <div className="flex-none border-b border-line bg-surface-2 px-5 py-2">
          <div className="flex items-center gap-3">
            <span className="font-mono text-[11px] text-muted">{selected.size} selected</span>
            <button
              onClick={handleBatchPublish}
              disabled={publish.isPending}
              className="inline-flex items-center gap-1 rounded-md border border-[#1c2620] bg-[rgba(95,208,122,0.08)] px-2.5 py-1 text-[11.5px] text-st-done transition-colors hover:bg-[rgba(95,208,122,0.14)] disabled:opacity-50"
            >
              <Check size={12} /> Publish all
            </button>
            <button
              onClick={handleBatchReject}
              disabled={reject.isPending}
              className="inline-flex items-center gap-1 rounded-md border border-line px-2.5 py-1 text-[11.5px] text-muted transition-colors hover:border-line-hover hover:text-ink disabled:opacity-50"
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
        </div>
      )}

      {/* Main content: row table + detail panel */}
      <div className="min-h-0 flex-1 overflow-hidden">
        <div className={cn("flex h-full", detail ? "flex-row" : "")}>
          {/* Row table */}
          <div className={cn("min-h-0 flex-1 overflow-y-auto", detail ? "w-1/2 border-r border-line" : "")}>
            {visibleRows.length === 0 ? (
              <div className="py-16 text-center text-[13px] text-muted">
                {queueCounts[activeQueue] === 0
                  ? `No shards in the ${QUEUES.find((q) => q.key === activeQueue)?.label.toLowerCase()} queue.`
                  : "No shards match the current filters."}
              </div>
            ) : (
              <div className="flex flex-col">
                {visibleRows.map((row, i) => (
                  <ShardRow
                    key={row.shard.id}
                    row={row}
                    isCursor={i === cursor}
                    isSelected={selected.has(row.shard.id)}
                    isDetail={detailId === row.shard.id}
                    onClick={() => { setCursor(i); setDetailId(row.shard.id); }}
                    onSelect={() => toggleSelect(row.shard.id)}
                    cluster={clusterById.get(row.shard.id)}
                  />
                ))}
                {hasMore && (
                  <button
                    onClick={() => setVisibleCount((c) => c + 50)}
                    className="border-t border-line py-3 text-center text-[12px] text-muted transition-colors hover:bg-surface-2 hover:text-ink"
                  >
                    Load {Math.min(50, queueRows.length - visibleCount)} more ({queueRows.length - visibleCount} remaining)
                  </button>
                )}
              </div>
            )}
          </div>

          {/* Detail panel */}
          {detail && (
            <DetailPanel
              row={detail}
              cluster={clusterById.get(detail.shard.id)}
              judgeOn={judgeOn}
              onClose={() => setDetailId(null)}
              onPublish={() => handlePublish(detail.shard.id, detail.shard.text)}
              onReject={() => handleReject(detail.shard.id, detail.shard.text)}
              onAskJudge={() => judge.mutate(detail.shard.id)}
              onUndoAuto={() => undoAuto.mutate(detail.shard.id)}
              busy={publish.isPending || reject.isPending}
              judgeResult={judge.data?.shard_id === detail.shard.id ? judge.data : null}
              judgePending={judge.isPending}
              allScored={scored ?? []}
            />
          )}
        </div>
      </div>

      {/* Sweep preview modal */}
      {sweepPreview && (
        <SweepPreviewModal
          rows={queueRows.filter(
            (r) => r.score && (r.score.suggestion === "reject" || r.score.confidence < 0.3),
          )}
          onConfirm={handleSweep}
          onClose={() => setSweepPreview(false)}
          busy={reject.isPending}
        />
      )}

      {/* Undo toast */}
      {undo && (
        <div className="fixed bottom-5 left-1/2 z-50 flex -translate-x-1/2 items-center gap-3 rounded-xl border border-line bg-surface-3 px-4 py-2.5 shadow-lg">
          <span className="text-[12.5px] text-ink">
            {undo.action === "published" ? "Published" : "Rejected"}:{" "}
            <span className="text-muted">{undo.text.slice(0, 60)}{undo.text.length > 60 ? "…" : ""}</span>
          </span>
          <button
            onClick={handleUndo}
            className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11px] text-muted transition-colors hover:border-line-hover hover:text-ink"
          >
            <RotateCcw size={11} /> Undo
          </button>
          <button onClick={() => setUndo(null)} className="text-faint hover:text-muted">
            <X size={14} />
          </button>
        </div>
      )}
    </div>
  );
}

function ShardRow({
  row,
  isCursor,
  isSelected,
  isDetail,
  onClick,
  onSelect,
  cluster,
}: {
  row: { shard: Shard; score?: ScoredCandidate };
  isCursor: boolean;
  isSelected: boolean;
  isDetail: boolean;
  onClick: () => void;
  onSelect: () => void;
  cluster?: ShardCluster;
}) {
  const { shard, score } = row;
  return (
    <div
      onClick={onClick}
      className={cn(
        "group flex items-start gap-3 border-b border-line px-5 py-3 cursor-pointer transition-colors",
        isCursor && "bg-surface-2",
        isDetail && "bg-surface-3/50",
        isSelected && "bg-[rgba(95,208,122,0.04)]",
        !isCursor && !isDetail && "hover:bg-surface-2/50",
      )}
    >
      <button
        onClick={(e) => { e.stopPropagation(); onSelect(); }}
        className={cn(
          "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors",
          isSelected
            ? "border-st-done bg-st-done/20"
            : "border-line group-hover:border-line-hover",
        )}
      >
        {isSelected && <Check size={10} className="text-st-done" />}
      </button>
      <div className="min-w-0 flex-1">
        <p className="truncate text-[13px] text-ink">{shard.text}</p>
        <div className="mt-1 flex items-center gap-2">
          <span className="font-mono text-[10px] text-faint">{shard.origin || "agent"}</span>
          {shard.source && <span className="font-mono text-[10px] text-faint">· {shard.source}</span>}
          {score && (
            <SuggestionBadge suggestion={score.suggestion} confidence={score.confidence} />
          )}
          {(score?.conflicts?.length ?? 0) > 0 && (
            <span className="inline-flex items-center gap-0.5 rounded border border-[rgba(255,107,107,0.3)] bg-[rgba(255,107,107,0.06)] px-1 py-0.5 font-mono text-[9px] text-st-blocked">
              <AlertTriangle size={9} /> {score!.conflicts!.length} conflict{score!.conflicts!.length > 1 ? "s" : ""}
            </span>
          )}
          {score?.duplicate_of && (
            <span className="rounded border border-line-2 bg-surface-3 px-1 py-0.5 font-mono text-[9px] text-faint">
              dupe
            </span>
          )}
          {cluster && (
            <span className="inline-flex items-center gap-0.5 rounded border border-[rgba(224,179,74,0.3)] bg-[rgba(224,179,74,0.06)] px-1 py-0.5 font-mono text-[9px] text-[#e0b34a]">
              <Layers size={9} /> ×{cluster.size}
            </span>
          )}
          {shard.scoring_source && UNVETTED_SOURCES.includes(shard.scoring_source) && (
            <span className="rounded border border-[rgba(167,139,250,0.3)] bg-[rgba(167,139,250,0.08)] px-1 py-0.5 font-mono text-[9px] text-[#a78bfa]">
              unvetted
            </span>
          )}
        </div>
      </div>
      <div className="shrink-0 text-right">
        {score?.confidence != null && (
          <span className="font-mono text-[10px] text-faint">{Math.round(score.confidence * 100)}%</span>
        )}
        <p className="mt-0.5 font-mono text-[9.5px] text-faint">{timeAgo(shard.created_at)}</p>
      </div>
    </div>
  );
}

function SuggestionBadge({ suggestion, confidence }: { suggestion: string; confidence: number }) {
  const meta: Record<string, { label: string; cls: string }> = {
    accept: { label: "suggest publish", cls: "border-[#1c2620] bg-[rgba(95,208,122,0.1)] text-st-done" },
    reject: { label: "suggest reject", cls: "border-[rgba(255,107,107,0.3)] bg-[rgba(255,107,107,0.08)] text-st-blocked" },
    review: { label: "needs look", cls: "border-[#3a2f1a] bg-[rgba(224,179,74,0.08)] text-[#e0b34a]" },
  };
  const m = meta[suggestion] ?? meta.review;
  return (
    <span title={`${Math.round(confidence * 100)}% confidence`} className={cn("rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide", m.cls)}>
      {m.label}
    </span>
  );
}

function DetailPanel({
  row,
  cluster,
  judgeOn,
  onClose,
  onPublish,
  onReject,
  onAskJudge,
  onUndoAuto,
  busy,
  judgeResult,
  judgePending,
  allScored,
}: {
  row: { shard: Shard; score?: ScoredCandidate };
  cluster?: ShardCluster;
  judgeOn: boolean;
  onClose: () => void;
  onPublish: () => void;
  onReject: () => void;
  onAskJudge: () => void;
  onUndoAuto: () => void;
  busy: boolean;
  judgeResult: CandidateJudge | null;
  judgePending: boolean;
  allScored: ScoredCandidate[];
}) {
  const { shard, score } = row;
  const [classPicker, setClassPicker] = React.useState(shard.source || "note");
  const contradictId = score?.conflicts?.[0] ?? null;
  const contradictShard = contradictId
    ? allScored.find((s) => s.shard.id === contradictId)
    : null;
  const dupeId = score?.duplicate_of ?? null;
  const dupeShard = dupeId
    ? allScored.find((s) => s.shard.id === dupeId)
    : null;

  return (
    <div className="flex h-full w-1/2 flex-col overflow-y-auto bg-surface-2/30">
      <div className="flex-none border-b border-line px-5 py-3">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[10.5px] text-faint">{shard.id.slice(0, 8)}</span>
          <span className="font-mono text-[10px] text-faint">{shard.created_at.slice(0, 10)}</span>
          <button onClick={onClose} className="ml-auto text-faint hover:text-muted">
            <X size={16} />
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4">
        {/* Shard text */}
        <div className="mb-4">
          <p className="whitespace-pre-wrap text-[13.5px] leading-relaxed text-ink">{shard.text}</p>
        </div>

        {/* Metadata */}
        <div className="mb-4 grid grid-cols-2 gap-x-4 gap-y-2 rounded-lg border border-line bg-surface-2/60 p-3">
          <MetaRow label="Status" value={shard.status} />
          <MetaRow label="Scope" value={shard.scope} />
          <MetaRow label="Origin" value={shard.origin || "—"} />
          <MetaRow label="Source" value={shard.source || "—"} />
          <MetaRow label="Fresh" value={shard.fresh ? "yes" : "no"} />
          {shard.auto_confidence != null && (
            <MetaRow label="Auto confidence" value={`${Math.round(shard.auto_confidence * 100)}%`} />
          )}
          {shard.scoring_source && (
            <MetaRow label="Scoring source" value={shard.scoring_source} />
          )}
          {shard.item_id && <MetaRow label="Item" value={shard.item_id} />}
        </div>

        {/* Class picker */}
        <div className="mb-4">
          <label className="mb-1 block font-mono text-[10px] uppercase tracking-wide text-faint">Class</label>
          <select
            value={classPicker}
            onChange={(e) => setClassPicker(e.target.value)}
            className="w-full rounded-lg border border-line bg-surface-2 px-3 py-1.5 text-[12px] text-ink focus:border-line-hover focus:outline-none"
          >
            <option value="note">Note</option>
            <option value="lesson">Lesson</option>
            <option value="decision">Decision</option>
            <option value="correction">Correction</option>
          </select>
        </div>

        {/* Contradicted published shard */}
        {contradictShard && (
          <div className="mb-4 rounded-lg border border-[rgba(255,107,107,0.3)] bg-[rgba(255,107,107,0.04)] p-3">
            <p className="mb-1 font-mono text-[10px] uppercase tracking-wide text-st-blocked">
              Contradicts published shard
            </p>
            <p className="text-[12.5px] text-ink">{contradictShard.shard.text}</p>
          </div>
        )}

        {/* Canonical wording picker for near-duplicates */}
        {(dupeShard || cluster) && (
          <div className="mb-4 rounded-lg border border-[rgba(224,179,74,0.3)] bg-[rgba(224,179,74,0.04)] p-3">
            <p className="mb-1 font-mono text-[10px] uppercase tracking-wide text-[#e0b34a]">
              Near-duplicate — pick canonical wording
            </p>
            {dupeShard && (
              <p className="mb-2 text-[12.5px] text-ink">{dupeShard.shard.text}</p>
            )}
            {cluster && cluster.members.length > 0 && (
              <div className="flex flex-col gap-1">
                <p className="text-[11.5px] text-muted">
                  This cluster has {cluster.size} similar shards. Promote the representative as canonical:
                </p>
                <p className="text-[12px] italic text-ink">{cluster.representative.text}</p>
              </div>
            )}
          </div>
        )}

        {/* Judge result */}
        {judgeResult?.verdict && (
          <div className="mb-4 rounded-lg border border-line bg-surface-2/60 p-3">
            <p className="mb-1 font-mono text-[10px] uppercase tracking-wide text-faint">Judge verdict</p>
            <p className="text-[12.5px] text-ink">
              {Math.round(judgeResult.verdict.quality * 100)}% quality — {judgeResult.verdict.reason || (judgeResult.verdict.keep ? "publish-worthy" : "not publish-worthy")}
            </p>
          </div>
        )}
        {judgeResult && !judgeResult.verdict && (
          <div className="mb-4 rounded-lg border border-line bg-surface-2/60 p-3">
            <p className="text-[12px] text-faint">Judge unavailable: {judgeResult.cause_detail}</p>
          </div>
        )}

        {/* Score info */}
        {score && (
          <div className="mb-4">
            {score.grounded != null && (
              <span className={cn(
                "mr-2 rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide",
                score.grounded
                  ? "border-line-2 bg-surface-3 text-muted"
                  : "border-[rgba(255,107,107,0.3)] bg-[rgba(255,107,107,0.08)] text-st-blocked",
              )}>
                {score.grounded ? "grounded" : "ungrounded"}
              </span>
            )}
            {score.ready != null && (
              <span className={cn(
                "rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide",
                score.ready
                  ? "border-line-2 bg-surface-3 text-muted"
                  : "border-[#3a2f1a] bg-[rgba(224,179,74,0.08)] text-[#e0b34a]",
              )}>
                {score.ready ? "ready" : "not ready"}
              </span>
            )}
            {score.reasons.length > 0 && (
              <p className="mt-2 text-[11.5px] text-faint">Why: {score.reasons.join(" · ")}</p>
            )}
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="flex-none border-t border-line px-5 py-3">
        <div className="flex items-center gap-2">
          <button
            onClick={onPublish}
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded-lg border border-[#1c2620] bg-[rgba(95,208,122,0.08)] px-3 py-1.5 text-[12px] font-medium text-st-done transition-colors hover:bg-[rgba(95,208,122,0.14)] disabled:opacity-50"
          >
            <Check size={13} /> Publish
          </button>
          <button
            onClick={onReject}
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-[12px] text-muted transition-colors hover:border-line-hover hover:text-ink disabled:opacity-50"
          >
            <X size={13} /> Reject
          </button>
          {judgeOn && (
            <button
              onClick={onAskJudge}
              disabled={busy}
              className="inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-[12px] text-muted transition-colors hover:border-line-hover hover:text-ink disabled:opacity-50"
            >
              {judgePending ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />}
              Ask the judge
            </button>
          )}
          {UNVETTED_SOURCES.includes(shard.scoring_source) && (
            <button
              onClick={onUndoAuto}
              disabled={busy}
              className="ml-auto inline-flex items-center gap-1 rounded-lg border border-line px-2.5 py-1.5 text-[11.5px] text-muted transition-colors hover:border-line-hover hover:text-ink disabled:opacity-50"
            >
              <RotateCcw size={12} /> Return to queue
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="font-mono text-[9.5px] uppercase tracking-wide text-faint">{label}</span>
      <p className="text-[12px] text-ink">{value}</p>
    </div>
  );
}

function SweepPreviewModal({
  rows,
  onConfirm,
  onClose,
  busy,
}: {
  rows: { shard: Shard; score?: ScoredCandidate }[];
  onConfirm: () => void;
  onClose: () => void;
  busy: boolean;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="mx-4 w-full max-w-lg rounded-xl border border-line bg-surface-2 p-5 shadow-xl">
        <h3 className="mb-2 text-[15px] font-semibold text-ink">Sweep preview</h3>
        <p className="mb-3 text-[12.5px] text-muted">
          This will reject {rows.length} shard{rows.length !== 1 ? "s" : ""} with low confidence or a "suggest reject" scoring. They will be kept for provenance but excluded from search.
        </p>
        {rows.length > 0 ? (
          <div className="mb-4 max-h-48 overflow-y-auto rounded-lg border border-line bg-surface-3/50 p-2">
            {rows.map((r) => (
              <div key={r.shard.id} className="border-b border-line/50 py-1.5 last:border-0">
                <p className="truncate text-[12px] text-ink">{r.shard.text}</p>
                <span className="font-mono text-[9.5px] text-faint">
                  {r.score?.suggestion} · {r.score?.confidence != null ? `${Math.round(r.score.confidence * 100)}%` : "—"}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <p className="mb-4 text-[12.5px] text-muted">Nothing to sweep in this queue.</p>
        )}
        <div className="flex items-center justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-lg border border-line px-3 py-1.5 text-[12px] text-muted transition-colors hover:border-line-hover hover:text-ink"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={busy || rows.length === 0}
            className="rounded-lg border border-[rgba(255,107,107,0.3)] bg-[rgba(255,107,107,0.08)] px-3 py-1.5 text-[12px] font-medium text-st-blocked transition-colors hover:bg-[rgba(255,107,107,0.14)] disabled:opacity-50"
          >
            Reject {rows.length} shard{rows.length !== 1 ? "s" : ""}
          </button>
        </div>
      </div>
    </div>
  );
}

function timeAgo(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d`;
  return `${Math.floor(days / 30)}mo`;
}
