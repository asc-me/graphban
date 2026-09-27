import { ArrowLeft, ArrowDown, ArrowRight, ArrowUp, Check, ChevronRight } from "lucide-react";
import * as React from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import {
  LessonsListSkeleton,
  PlannerError,
} from "@/components/planner/PlannerStates";
import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { useProjectCtx } from "@/features/ProjectContext";
import {
  useLesson,
  useLessons,
  usePromoteOrgLesson,
  useRecordLessonOutcome,
} from "@/lib/queries";
import { projectPath, tagFromPath } from "@/lib/routes";
import type {
  LessonDetail,
  LessonListRow,
} from "@/lib/types";

type QueueId = "dropping" | "missed" | "unmeasured" | "overlap" | "promote" | "unclassified";

const QUEUE_META: Record<QueueId, { label: string; hint: string }> = {
  dropping: { label: "Dropping", hint: "Effectiveness is falling — what was catching issues is no longer" },
  missed: { label: "Missed", hint: "Surfaced but the issue still happened — the lesson did not catch it" },
  unmeasured: { label: "Unmeasured", hint: "No outcomes recorded — a hit cannot be told apart from noise" },
  overlap: { label: "Overlap", hint: "Near-duplicates — keep one, the rest retire into it" },
  promote: { label: "Promote", hint: "Eligible for org-wide reach but not yet promoted" },
  unclassified: { label: "Unclassified", hint: "Does not fit the other queues — still published, still counted" },
};

const QUEUE_ORDER: QueueId[] = ["dropping", "missed", "unmeasured", "overlap", "promote", "unclassified"];

/** Published catalog. Memory review is the candidate inbox — different empty, different job. */
export function LessonsView() {
  const { id } = useParams();
  if (id) return <LessonDetailPage id={id} />;
  return <LessonListPage />;
}

function partitionQueues(rows: LessonListRow[]): Map<QueueId, LessonListRow[]> {
  const queues = new Map<QueueId, LessonListRow[]>();
  for (const q of QUEUE_ORDER) queues.set(q, []);

  const byOrigin = new Map<string, LessonListRow[]>();
  for (const r of rows) {
    const key = r.item_id
      ? `item:${r.item_id}`
      : r.source
        ? `source:${r.source}`
        : null;
    if (key) {
      const group = byOrigin.get(key) ?? [];
      group.push(r);
      byOrigin.set(key, group);
    }
  }
  const overlapIds = new Set<string>();
  for (const [, group] of byOrigin) {
    if (group.length > 1) {
      for (const r of group) overlapIds.add(r.id);
    }
  }

  for (const r of rows) {
    if (overlapIds.has(r.id)) {
      queues.get("overlap")!.push(r);
    } else if (r.effectiveness?.trend === "dropping") {
      queues.get("dropping")!.push(r);
    } else if (r.caught_state === "missed" || r.caught_state === "mixed") {
      queues.get("missed")!.push(r);
    } else if (!r.caught_state || r.caught_state === "unknown") {
      queues.get("unmeasured")!.push(r);
    } else if (r.eligibility?.state === "eligible" && r.reach !== "org") {
      queues.get("promote")!.push(r);
    } else {
      queues.get("unclassified")!.push(r);
    }
  }
  return queues;
}

function LessonListPage() {
  const { activeId } = useProjectCtx();
  const catalogQ = useLessons(activeId);
  const { isLoading, isError: catalogError, refetch } = catalogQ;
  const failed = catalogQ.isError || catalogError;

  const allRows = catalogQ.data?.results ?? [];
  const published = catalogQ.data?.total ?? 0;
  const hasMore = catalogQ.data?.has_more ?? false;

  const queues = React.useMemo(() => partitionQueues(allRows), [allRows]);
  const [activeQueue, setActiveQueue] = React.useState<QueueId>("dropping");
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [focusIdx, setFocusIdx] = React.useState(0);
  const queueRef = React.useRef<HTMLDivElement>(null);

  const currentRows = queues.get(activeQueue) ?? [];
  const empty = !isLoading && !failed && published === 0;

  React.useEffect(() => {
    setFocusIdx(0);
  }, [activeQueue]);

  const handleKeyDown = React.useCallback((e: React.KeyboardEvent) => {
    if (e.key === "j" || e.key === "J") {
      e.preventDefault();
      setFocusIdx((i) => Math.min(i + 1, currentRows.length - 1));
    } else if (e.key === "k" || e.key === "K") {
      e.preventDefault();
      setFocusIdx((i) => Math.max(i - 1, 0));
    } else if (e.key === "x" || e.key === "X") {
      e.preventDefault();
      const row = currentRows[focusIdx];
      if (row) {
        setSelected((prev) => {
          const next = new Set(prev);
          if (next.has(row.id)) next.delete(row.id);
          else next.add(row.id);
          return next;
        });
      }
    } else if (e.key === "Enter") {
      e.preventDefault();
      const row = currentRows[focusIdx];
      if (row) {
        const tag = tagFromPath(window.location.pathname);
        const base = tag ? `/p/${tag}/lessons/${row.id}` : `/lessons/${row.id}`;
        window.location.href = base;
      }
    }
  }, [currentRows, focusIdx]);

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectAll = () => {
    if (selected.size === currentRows.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(currentRows.map((r) => r.id)));
    }
  };

  const queueCounts = React.useMemo(() => {
    const m: Record<string, number> = {};
    for (const q of QUEUE_ORDER) m[q] = queues.get(q)?.length ?? 0;
    return m;
  }, [queues]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PlaceHeader
        viewName="Lessons"
        purpose="Published memory, scored against whether it is still catching anything. Candidates stay in Memory until you publish them."
        action={
          <div className="flex items-center gap-3 font-mono text-[10.5px] text-faint">
            {isLoading ? (
              <>
                <span className="h-3 w-16 animate-pulse rounded bg-surface-3" />
                <span className="h-3 w-20 animate-pulse rounded bg-surface-3" />
              </>
            ) : (
              <>
                <span>{published} PUBLISHED</span>
                <span>{queueCounts.unmeasured} UNMEASURED{hasMore ? " THIS PAGE" : ""}</span>
                <span>{queueCounts.dropping} DROPPING{hasMore ? " THIS PAGE" : ""}</span>
              </>
            )}
          </div>
        }
      />

      <QueuePicker
        active={activeQueue}
        counts={queueCounts}
        onChange={setActiveQueue}
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading ? (
          <LessonsListSkeleton />
        ) : failed ? (
          <PlannerError message="The lesson catalog could not be loaded." onRetry={() => {
            void refetch();
            void catalogQ.refetch();
          }} />
        ) : (
          <div className="mx-auto flex max-w-3xl flex-col gap-2.5 p-5">
            {empty ? (
              <EmptyCatalog />
            ) : (
              <>
                {queueCounts.unmeasured > 0 && (
                  <div className="rounded-[10px] border border-[#3a2f1a] bg-[rgba(224,179,74,0.08)] px-3.5 py-2.5 text-[12.5px] leading-relaxed text-[#e0b34a]">
                    {hasMore
                      ? `At least ${queueCounts.unmeasured} of this page of published lessons have no outcomes yet.`
                      : `${published} published lesson${published === 1 ? " has" : "s have"} no outcomes yet.`}{" "}
                    That is <span className="font-semibold">unknown</span>, not effective — nothing has
                    caught or missed since they were published.
                  </div>
                )}
                {hasMore && (
                  <p className="text-[12px] text-faint">
                    More lessons exist beyond this page — counts above are this page, not the rest of
                    the catalog.
                  </p>
                )}
                {currentRows.length === 0 ? (
                  <QueueEmpty queue={activeQueue} />
                ) : (
                  <>
                    <BulkBar
                      count={selected.size}
                      total={currentRows.length}
                      onSelectAll={selectAll}
                      allSelected={selected.size === currentRows.length}
                    />
                    <div
                      ref={queueRef}
                      tabIndex={0}
                      className="flex flex-col gap-1.5 outline-none"
                      onKeyDown={handleKeyDown}
                      role="listbox"
                      aria-label={`${QUEUE_META[activeQueue].label} queue`}
                    >
                      {currentRows.map((row, idx) => (
                        <QueueRow
                          key={row.id}
                          row={row}
                          idx={idx}
                          focused={idx === focusIdx}
                          selected={selected.has(row.id)}
                          onToggleSelect={() => toggleSelect(row.id)}
                          onFocus={() => setFocusIdx(idx)}
                        />
                      ))}
                    </div>
                    <p className="py-2 text-center font-mono text-[10.5px] text-faint">
                      J/K move · X select · Enter open
                    </p>
                  </>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function QueuePicker({
  active,
  counts,
  onChange,
}: {
  active: QueueId;
  counts: Record<string, number>;
  onChange: (q: QueueId) => void;
}) {
  return (
    <div className="flex flex-none flex-wrap items-center gap-1 border-b border-line px-5 py-2">
      {QUEUE_ORDER.map((q) => {
        const count = counts[q] ?? 0;
        const isActive = q === active;
        return (
          <button
            key={q}
            type="button"
            role="tab"
            aria-selected={isActive}
            title={QUEUE_META[q].hint}
            onClick={() => onChange(q)}
            className={cn(
              "rounded-md border px-2 py-1 font-mono text-[10px] uppercase tracking-wide transition-colors",
              isActive
                ? "border-line-hover bg-surface-3 text-fg"
                : "border-line-2 text-faint hover:border-line-hover hover:text-muted",
            )}
          >
            {QUEUE_META[q].label}
            <span className={cn("ml-1.5", count === 0 ? "text-faint" : "text-muted")}>
              {count}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function BulkBar({
  count,
  total,
  onSelectAll,
  allSelected,
}: {
  count: number;
  total: number;
  onSelectAll: () => void;
  allSelected: boolean;
}) {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-line-2 bg-surface-2 px-3 py-2">
      <button
        type="button"
        onClick={onSelectAll}
        className={cn(
          "flex h-4 w-4 items-center justify-center rounded border transition-colors",
          allSelected
            ? "border-st-done bg-st-done/10 text-st-done"
            : "border-line-2 hover:border-line-hover",
        )}
        aria-label={allSelected ? "Deselect all" : "Select all"}
      >
        {allSelected && <Check size={10} />}
      </button>
      <span className="font-mono text-[10.5px] text-faint">
        {count === 0
          ? `${total} in queue`
          : `${count} of ${total} selected`}
      </span>
    </div>
  );
}

function QueueRow({
  row,
  idx,
  focused,
  selected,
  onToggleSelect,
  onFocus,
}: {
  row: LessonListRow;
  idx: number;
  focused: boolean;
  selected: boolean;
  onToggleSelect: () => void;
  onFocus: () => void;
}) {
  const { projects, activeId } = useProjectCtx();
  const originTag =
    row.project_id && row.project_id !== activeId
      ? projects.find((p) => p.id === row.project_id)?.tag
      : null;
  return (
    <Link
      to={row.id}
      data-row-idx={idx}
      tabIndex={0}
      onFocus={onFocus}
      className={cn(
        "flex items-start gap-2.5 rounded-[10px] border px-3.5 py-3 transition-colors outline-none",
        focused
          ? "border-line-hover bg-surface-2 ring-1 ring-line-hover"
          : "border-line-2 bg-surface-2 hover:border-line-hover",
      )}
    >
      <button
        type="button"
        role="checkbox"
        aria-checked={selected}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onToggleSelect();
        }}
        className={cn(
          "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors",
          selected
            ? "border-st-done bg-st-done/10 text-st-done"
            : "border-line-2 hover:border-line-hover",
        )}
      >
        {selected && <Check size={10} />}
      </button>
      <div className="min-w-0 flex-1">
        <p className="line-clamp-2 text-[13px] leading-relaxed text-ink">{row.text}</p>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <ClassChip lessonClass={row.lesson_class} suggested={row.suggested_class} />
          <span className="font-mono text-[10.5px] text-faint">
            {row.source || row.origin || "—"}
            {row.item_id ? ` · ${row.item_id}` : ""}
          </span>
          {row.reach === "org" && (
            <Chip tone="accent">
              {row.transferability === "overridden"
                ? "org (overridden)"
                : originTag
                  ? `org · from ${originTag}`
                  : "org"}
            </Chip>
          )}
          {originTag && row.reach !== "org" && (
            <Chip tone="muted">from {originTag}</Chip>
          )}
          <CaughtChip state={row.caught_state} />
          <ScoreChip row={row} />
          <OutcomeStrip row={row} />
        </div>
      </div>
      <ChevronRight size={14} className="mt-1 shrink-0 text-faint" />
    </Link>
  );
}

function OutcomeStrip({ row }: { row: LessonListRow }) {
  const score = row.effectiveness?.score;
  const trend = row.effectiveness?.trend ?? "unmeasured";
  const caught = row.caught_state;
  const hasOutcomes = caught && caught !== "unknown";

  if (!hasOutcomes && score == null && trend === "unmeasured") {
    return (
      <span
        className="inline-flex items-center gap-0.5 font-mono text-[9px] text-faint"
        aria-label="outcome trend: unmeasured"
        title="No outcomes recorded. Nothing links this lesson to a check, so a hit can't be told apart from noise."
      >
        {Array.from({ length: 8 }, (_, i) => (
          <span key={i} className="h-2.5 w-1 rounded-sm bg-line-2" />
        ))}
      </span>
    );
  }

  const bars = 8;
  const segments: { color: string }[] = [];
  if (trend === "dropping") {
    for (let i = 0; i < bars; i++) {
      const ratio = i / (bars - 1);
      segments.push({
        color: ratio < 0.4
          ? "bg-st-done/60"
          : ratio < 0.7
            ? "bg-[#e0b34a]/60"
            : "bg-st-blocked/60",
      });
    }
  } else if (trend === "rising") {
    for (let i = 0; i < bars; i++) {
      const ratio = i / (bars - 1);
      segments.push({
        color: ratio < 0.3
          ? "bg-faint"
          : ratio < 0.6
            ? "bg-[#e0b34a]/60"
            : "bg-st-done/60",
      });
    }
  } else if (trend === "stable") {
    const color = caught === "caught"
      ? "bg-st-done/60"
      : caught === "missed" || caught === "mixed"
        ? "bg-st-blocked/60"
        : "bg-faint";
    for (let i = 0; i < bars; i++) segments.push({ color });
  } else {
    for (let i = 0; i < bars; i++) segments.push({ color: "bg-faint" });
  }

  return (
    <span className="inline-flex items-center gap-px" aria-label={`outcome trend: ${trend}`}>
      {segments.map((s, i) => (
        <span key={i} className={cn("h-2.5 w-1 rounded-sm", s.color)} />
      ))}
    </span>
  );
}

function QueueEmpty({ queue }: { queue: QueueId }) {
  const messages: Record<QueueId, string> = {
    dropping: "Nothing is dropping. Lessons with declining effectiveness would appear here.",
    missed: "Nothing missed yet. Lessons that surfaced but failed to catch would appear here.",
    unmeasured: "Every lesson has been measured. Unmeasured ones would appear here.",
    overlap: "No near-duplicates detected. Overlapping lessons would appear here for retire-into resolution.",
    promote: "Nothing is waiting for promotion. Eligible lessons not yet org-wide would appear here.",
    unclassified: "Queue clear. Pick another queue above.",
  };
  return (
    <div className="py-16 text-center text-[13px] leading-relaxed text-muted">
      {messages[queue]}
    </div>
  );
}

function EmptyCatalog() {
  const { active } = useProjectCtx();
  const { pathname } = useLocation();
  const tag = tagFromPath(pathname);
  const memoryTo = tag && active?.tag ? projectPath(active.tag, "memory-review") : "/memory-review";
  return (
    <div className="py-16 text-center text-[13px] leading-relaxed text-muted">
      No published lessons in this project. Agent-written notes queue in{" "}
      <Link to={memoryTo} className="text-fg underline decoration-line-hover underline-offset-2 hover:text-ink">
        Memory
      </Link>{" "}
      until you publish them. This page is the catalog of what you have already stood behind — it is
      not a scoreboard, and an empty catalog is not a high score.
    </div>
  );
}

function ScoreChip({ row }: { row: LessonListRow }) {
  const score = row.effectiveness?.score;
  const trend = row.effectiveness?.trend ?? "unmeasured";
  return (
    <span className="inline-flex items-center gap-1 font-mono text-[10.5px] text-faint">
      <span>{score == null ? "—" : score.toFixed(1)}</span>
      <TrendGlyph trend={trend} />
    </span>
  );
}

function TrendGlyph({ trend }: { trend: string }) {
  if (trend === "dropping") return <ArrowDown size={11} className="text-st-blocked" aria-label="dropping" />;
  if (trend === "rising") return <ArrowUp size={11} className="text-st-done" aria-label="rising" />;
  if (trend === "stable") return <ArrowRight size={11} className="text-faint" aria-label="stable" />;
  return <span aria-label="unmeasured">—</span>;
}

function ClassChip({ lessonClass, suggested }: { lessonClass: string; suggested: string | null }) {
  const stored = lessonClass || "";
  return (
    <span
      title={suggested ? `looks like a ${suggested}` : undefined}
      className={cn(
        "rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide",
        stored
          ? "border-line-2 text-muted"
          : "border-line-2 text-faint",
      )}
    >
      {stored || "unclassified"}
    </span>
  );
}

function CaughtChip({ state }: { state: string | undefined }) {
  const value = state || "unknown";
  const tone =
    value === "caught" ? "done" : value === "missed" ? "blocked" : value === "mixed" ? "review" : "muted";
  return <Chip tone={tone}>{value}</Chip>;
}

function Chip({
  children,
  tone,
  title,
}: {
  children: React.ReactNode;
  tone: "done" | "blocked" | "review" | "accent" | "muted";
  title?: string;
}) {
  const cls = {
    done: "border-[#1c2620] bg-[rgba(95,208,122,0.1)] text-st-done",
    blocked: "border-[rgba(255,107,107,0.3)] bg-[rgba(255,107,107,0.08)] text-st-blocked",
    review: "border-[#3a2f1a] bg-[rgba(224,179,74,0.08)] text-[#e0b34a]",
    accent: "border-[rgba(167,139,250,0.35)] bg-[rgba(167,139,250,0.1)] text-[#a78bfa]",
    muted: "border-line-2 text-faint",
  }[tone];
  return (
    <span title={title} className={cn("rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide", cls)}>
      {children}
    </span>
  );
}

function LessonDetailPage({ id }: { id: string }) {
  const { activeId } = useProjectCtx();
  const navigate = useNavigate();
  const { data, isLoading, isError, refetch } = useLesson(activeId, id);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-none items-center gap-3 border-b border-line px-5 py-4">
        <button
          type="button"
          onClick={() => navigate("..")}
          className="text-[12.5px] text-muted hover:text-fg-2"
        >
          ← Lessons
        </button>
        <h1 className="text-[18px] font-semibold tracking-tight">Lesson</h1>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading ? (
          <LessonsListSkeleton rows={3} />
        ) : isError || !data ? (
          <PlannerError message="Lesson not found." onRetry={() => refetch()} />
        ) : (
          <LessonDetailBody lesson={data} />
        )}
      </div>
    </div>
  );
}

function LessonDetailBody({ lesson }: { lesson: LessonDetail }) {
  const navigate = useNavigate();
  const score = lesson.effectiveness?.score;
  const trend =
    lesson.origin_path === "gone"
      ? (lesson.effectiveness?.trend ?? "dropping")
      : (lesson.effectiveness?.trend ?? "unmeasured");
  const history = lesson.effectiveness?.history ?? [];
  const dropReasons = lesson.effectiveness?.drop_reasons ?? [];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-none items-center gap-3 border-b border-line px-5 py-4">
        <button
          type="button"
          onClick={() => navigate("..")}
          className="rounded-md p-1 text-muted hover:bg-surface-3 hover:text-fg"
          aria-label="Back to Lessons"
        >
          <ArrowLeft size={16} />
        </button>
        <div className="min-w-0 flex-1">
          <h1 className="text-[18px] font-semibold tracking-tight">Lesson</h1>
          <p className="mt-0.5 font-mono text-[11px] text-faint">{lesson.id}</p>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        <div className="mx-auto flex max-w-3xl flex-col gap-3">
          <section className="rounded-[10px] border border-line-2 bg-surface-2 p-3.5">
            <div className="mb-2 flex flex-wrap items-center gap-1.5">
              <ClassChip lessonClass={lesson.lesson_class} suggested={lesson.suggested_class} />
              <Chip tone="muted">{lesson.age_state}</Chip>
              {lesson.reach === "org" && (
                <Chip tone="accent">
                  {lesson.transferability === "overridden" ? "org (overridden)" : "org"}
                </Chip>
              )}
            </div>
            <p className="whitespace-pre-wrap text-[13.5px] leading-relaxed text-ink">{lesson.text}</p>
            <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-faint">
              {lesson.originating_item ? (
                <span>
                  item{" "}
                  <span className="text-muted">{lesson.originating_item.id}</span>
                  {lesson.originating_item.title ? ` · ${lesson.originating_item.title}` : ""}
                </span>
              ) : lesson.source?.startsWith("transcript:") ? (
                <span>no originating item — ingested from a transcript</span>
              ) : (
                <span>no originating item</span>
              )}
              {lesson.source && <span>source {lesson.source}</span>}
              {lesson.origin && <span>origin {lesson.origin}</span>}
              {lesson.scoring_source !== undefined && lesson.scoring_source !== "" && (
                <span>scoring_source {lesson.scoring_source}</span>
              )}
            </div>
          </section>

          <section className="rounded-[10px] border border-line-2 bg-surface-2 p-3.5">
            <div className="mb-2 font-mono text-[10.5px] uppercase tracking-wide text-faint">Judgement</div>
            <div className="flex flex-wrap items-center gap-2">
              <CaughtChip state={lesson.caught_state} />
              <span className="font-mono text-[12px] text-fg-2">
                {score == null ? "unmeasured" : score.toFixed(1)}
              </span>
              <TrendGlyph trend={trend} />
              <span className="font-mono text-[10.5px] uppercase text-faint">{trend}</span>
            </div>
            {dropReasons.length > 0 && (
              <ul className="mt-2 flex flex-col gap-1 text-[12.5px] text-muted">
                {dropReasons.map((r) => (
                  <li key={r}>{dropReasonCopy(r)}</li>
                ))}
              </ul>
            )}
            {lesson.origin_path === "unindexed" && (
              <p className="mt-2 text-[12.5px] text-muted">
                code graph has not been indexed — not the same as a deleted path.
              </p>
            )}
            {lesson.origin_path === "gone" && !dropReasons.includes("origin_path_gone") && (
              <p className="mt-2 text-[12.5px] text-muted">
                originating code path no longer resolves
              </p>
            )}
          </section>

          <details className="rounded-[10px] border border-line-2 bg-surface-2 p-3.5">
            <summary className="cursor-pointer select-none font-mono text-[10.5px] uppercase tracking-wide text-faint">
              Provenance
            </summary>
            <Provenance lesson={lesson} />
          </details>

          <details className="rounded-[10px] border border-line-2 bg-surface-2 p-3.5">
            <summary className="cursor-pointer select-none font-mono text-[10.5px] uppercase tracking-wide text-faint">
              Corroborating shards
            </summary>
            <ClusterSection lesson={lesson} />
          </details>

          <details className="rounded-[10px] border border-line-2 bg-surface-2 p-3.5">
            <summary className="cursor-pointer select-none font-mono text-[10.5px] uppercase tracking-wide text-faint">
              Outcomes
            </summary>
            <OutcomesSection lesson={lesson} />
          </details>

          <details className="rounded-[10px] border border-line-2 bg-surface-2 p-3.5">
            <summary className="cursor-pointer select-none font-mono text-[10.5px] uppercase tracking-wide text-faint">
              Effectiveness history
            </summary>
            {history.length === 0 ? (
              <p className="mt-2 text-[12.5px] text-muted">
                No counted outcomes — history is empty, not a flat high score.
              </p>
            ) : (
              <HistorySpark history={history} />
            )}
          </details>

          <PromotePanel lesson={lesson} />
        </div>
      </div>
    </div>
  );
}

function Provenance({ lesson }: { lesson: LessonDetail }) {
  const events = lesson.events ?? [];
  if (events.length === 0 && !lesson.originating_item && !lesson.source) {
    return <p className="mt-2 text-[12.5px] text-muted">No provenance events recorded.</p>;
  }
  return (
    <ul className="mt-2 flex flex-col gap-1.5 border-l border-line pl-3 text-[12.5px] text-muted">
      {lesson.originating_item && (
        <li>
          originating item <span className="font-mono text-fg-2">{lesson.originating_item.id}</span>
        </li>
      )}
      {!lesson.originating_item && lesson.source?.startsWith("transcript:") && (
        <li>no originating item — ingested from a transcript</li>
      )}
      {lesson.source && !lesson.source.startsWith("transcript:") && !lesson.item_id && (
        <li>no session id — source is a category placeholder</li>
      )}
      {events.map((e, i) => (
        <li key={`${e.action}-${i}`}>
          <span className="font-mono text-accent">{e.action}</span>
          {e.actor_label ? ` · ${e.actor_label}` : ""}
          {e.ts ? ` · ${e.ts}` : ""}
        </li>
      ))}
    </ul>
  );
}

function ClusterSection({ lesson }: { lesson: LessonDetail }) {
  const scan = lesson.eligibility?.cluster_scan;
  const others = (lesson.cluster ?? []).filter((s) => s.id !== lesson.id);
  const unread = lesson.unread_cluster_tags ?? [];
  if (scan !== "scanned") {
    return (
      <p className="mt-2 text-[12.5px] text-muted">
        Other-project recurrence was not scanned. That is <span className="font-medium text-fg-2">unverifiable</span>,
        not ineligible.
      </p>
    );
  }
  if (others.length === 0 && unread.length === 0) {
    return <p className="mt-2 text-[12.5px] text-muted">no corroborating shards</p>;
  }
  return (
    <div className="mt-2 flex flex-col gap-1.5">
      {others.length > 0 && (
        <p className="text-[11.5px] text-faint">
          Keep one — the rest retire into it.
        </p>
      )}
      {others.map((s) => (
        <div key={s.id} className="rounded-md border border-line-2 px-2.5 py-1.5">
          <p className="line-clamp-2 text-[12.5px] text-fg-2">{s.text}</p>
          <p className="mt-0.5 font-mono text-[10px] text-faint">
            {s.origin || ""} {s.source ? `· ${s.source}` : ""} {s.status}
          </p>
        </div>
      ))}
      {unread.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {unread.map((_, i) => (
            <Chip key={i} tone="muted">unread project</Chip>
          ))}
        </div>
      )}
    </div>
  );
}

function OutcomesSection({ lesson }: { lesson: LessonDetail }) {
  const { activeId } = useProjectCtx();
  const record = useRecordLessonOutcome(activeId);
  const [kind, setKind] = React.useState("caught");
  const [detail, setDetail] = React.useState("");
  const [open, setOpen] = React.useState(false);
  const outcomes = lesson.outcomes ?? [];
  return (
    <div className="mt-2 flex flex-col gap-2">
      {outcomes.length === 0 ? (
        <p className="text-[12.5px] text-muted">No outcomes recorded.</p>
      ) : (
        <table className="w-full text-left text-[12px]">
          <thead className="font-mono text-[10px] uppercase tracking-wide text-faint">
            <tr>
              <th className="py-1 pr-3 font-medium">kind</th>
              <th className="py-1 pr-3 font-medium">source</th>
              <th className="py-1 pr-3 font-medium">at</th>
              <th className="py-1 font-medium">detail</th>
            </tr>
          </thead>
          <tbody>
            {outcomes.map((o) => (
              <tr key={o.id} className="border-t border-line-2 text-muted">
                <td className="py-1.5 pr-3 font-mono text-fg-2">{o.kind}</td>
                <td className="py-1.5 pr-3">{o.source}</td>
                <td className="py-1.5 pr-3 font-mono text-[11px]">{o.created_at}</td>
                <td className="py-1.5">{o.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {open ? (
        <form
          className="flex flex-col gap-2 rounded-md border border-line-2 p-2.5"
          onSubmit={(e) => {
            e.preventDefault();
            if (!detail.trim()) return;
            record.mutate(
              { id: lesson.id, kind, detail: detail.trim() },
              { onSuccess: () => { setDetail(""); setOpen(false); } },
            );
          }}
        >
          <div className="flex gap-1.5">
            {["caught", "missed", "contradicted"].map((k) => (
              <FilterChip key={k} label={k} active={kind === k} onClick={() => setKind(k)} />
            ))}
          </div>
          <textarea
            required
            value={detail}
            onChange={(e) => setDetail(e.target.value)}
            placeholder="What happened — required"
            className="min-h-[64px] rounded-md border border-line-2 bg-surface-3 px-2 py-1.5 text-[12.5px] text-ink outline-none focus:border-line-hover"
          />
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={record.isPending || !detail.trim()}
              className="rounded-lg border border-[#1c2620] bg-[rgba(95,208,122,0.08)] px-2.5 py-1.5 text-[12px] font-medium text-st-done disabled:opacity-50"
            >
              Save outcome
            </button>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="rounded-lg border border-line px-2.5 py-1.5 text-[12px] text-muted"
            >
              Cancel
            </button>
          </div>
          <MutationError err={record.error} fallback="Could not record outcome." />
        </form>
      ) : (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="self-start rounded-lg border border-line px-2.5 py-1.5 text-[12px] text-muted hover:border-line-hover hover:text-ink"
        >
          Record outcome
        </button>
      )}
      {!open && <MutationError err={record.error} fallback="Could not record outcome." />}
    </div>
  );
}

function FilterChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "rounded-md border px-1.5 py-0.5 font-mono text-[9.5px] uppercase tracking-wide transition-colors",
        active
          ? "border-line-hover bg-surface-3 text-fg"
          : "border-line-2 text-faint hover:border-line-hover hover:text-muted",
      )}
    >
      {label}
    </button>
  );
}

function HistorySpark({ history }: { history: { at: string; score: number | null }[] }) {
  const scores = history.map((h) => h.score).filter((s): s is number => s != null);
  if (scores.length === 0) {
    return (
      <p className="mt-2 text-[12.5px] text-muted">
        No counted outcomes — history is empty, not a flat high score.
      </p>
    );
  }
  const w = 280;
  const h = 48;
  const min = Math.min(...scores, 0);
  const max = Math.max(...scores, 1);
  const span = max - min || 1;
  const pts = scores
    .map((s, i) => {
      const x = scores.length === 1 ? w / 2 : (i / (scores.length - 1)) * w;
      const y = h - ((s - min) / span) * (h - 4) - 2;
      return `${x},${y}`;
    })
    .join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="mt-2 h-12 w-full text-accent" aria-hidden>
      <polyline fill="none" stroke="currentColor" strokeWidth="1.5" points={pts} />
    </svg>
  );
}

function PromotePanel({ lesson }: { lesson: LessonDetail }) {
  const { activeId } = useProjectCtx();
  const promote = usePromoteOrgLesson(activeId);
  const [reason, setReason] = React.useState("");
  const elig = lesson.eligibility;
  const missingElig = !elig || !elig.state;
  const reasonText = elig?.reason || (missingElig ? "eligibility was not returned" : "");

  if (lesson.reach === "org") {
    const overridden = lesson.transferability === "overridden";
    const evidenced = lesson.transferability === "evidenced";
    return (
      <section className="rounded-[10px] border border-line-2 bg-surface-2 p-3.5">
        <div className="font-mono text-[10.5px] uppercase tracking-wide text-faint">Org promotion</div>
        <div className="mt-2">
          <Chip tone="accent">
            {overridden ? "org (overridden)" : evidenced ? "org · evidenced" : "org"}
          </Chip>
        </div>
        <p className="mt-2 text-[12.5px] text-muted">
          {overridden
            ? "Promoted with a written override — the independence formula did not pass."
            : evidenced
              ? "Already org-reach. Visible on sibling projects via retrieval widening."
              : "transferability was not recorded"}
        </p>
      </section>
    );
  }

  const failing = isFailing(lesson);
  const state = elig?.state;
  const unverifiable = missingElig || state === "unverifiable";
  const ineligible = state === "ineligible";
  const eligible = state === "eligible";
  const canDirect = eligible && !failing;
  const canOverride = ineligible || (eligible && failing);

  return (
    <section className="rounded-[10px] border border-line-2 bg-surface-2 p-3.5">
      <div className="font-mono text-[10.5px] uppercase tracking-wide text-faint">Org promotion</div>
      {reasonText && (
        <p className="mt-2 text-[12.5px] leading-relaxed text-muted" title={reasonText}>
          {reasonText}
        </p>
      )}
      {unverifiable && (
        <p className="mt-2 text-[12.5px] leading-relaxed text-muted">
          Cannot tell whether this can be an org lesson: <span className="font-medium text-fg-2">distinct_users</span>{" "}
          and/or <span className="font-medium text-fg-2">distinct_projects</span> are unmeasured, or the published
          cluster was not scanned across sibling projects. Ingest still writes every transcript to one project. That
          is not &quot;ineligible.&quot;
        </p>
      )}
      <div className="mt-3 flex flex-wrap items-end gap-2">
        <button
          type="button"
          disabled={!canDirect || promote.isPending}
          title={canDirect ? "Promote to org" : reasonText}
          onClick={() => promote.mutate({ id: lesson.id })}
          className="inline-flex items-center rounded-lg border border-[#1c2620] bg-[rgba(95,208,122,0.08)] px-2.5 py-1.5 text-[12px] font-medium text-st-done transition-colors hover:bg-[rgba(95,208,122,0.14)] disabled:cursor-not-allowed disabled:opacity-50"
        >
          Promote to org
        </button>
        {canOverride && (
          <div className="flex min-w-[220px] flex-1 flex-col gap-1.5">
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Override reason — required"
              className="rounded-md border border-line-2 bg-surface-3 px-2 py-1.5 text-[12.5px] text-ink outline-none focus:border-line-hover"
            />
            <button
              type="button"
              disabled={!reason.trim() || promote.isPending}
              onClick={() =>
                promote.mutate({ id: lesson.id, override_reason: reason.trim() })
              }
              className="self-start rounded-lg border border-line px-2.5 py-1.5 text-[12px] text-muted hover:border-line-hover hover:text-ink disabled:opacity-50"
            >
              Override with reason
            </button>
          </div>
        )}
      </div>
      <MutationError err={promote.error} fallback="Promote was refused." />
    </section>
  );
}

function isFailing(lesson: LessonDetail): boolean {
  if (lesson.origin_path === "gone") return true;
  const trend = lesson.effectiveness?.trend;
  const caught = lesson.caught_state;
  if (trend === "dropping") return true;
  if (caught === "missed" || caught === "mixed") return true;
  return (lesson.outcomes ?? []).some((o) => o.kind === "contradicted");
}

function MutationError({ err, fallback }: { err: unknown; fallback: string }) {
  if (!err) return null;
  return <p className="mt-2 text-[12.5px] text-st-blocked">{errorDetail(err, fallback)}</p>;
}

function dropReasonCopy(reason: string): string {
  if (reason === "contradicted") return "contradicted by a later incident of the same class";
  if (reason === "applied_and_recurred") return "applied and the issue still happened";
  if (reason === "origin_path_gone") return "originating code path no longer resolves";
  if (reason === "quiet_while_defects") {
    return "corroboration went quiet while similar defects continued";
  }
  return reason.replace(/_/g, " ");
}
