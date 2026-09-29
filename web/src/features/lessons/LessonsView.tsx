import { ArrowLeft, ArrowDown, ArrowRight, ArrowUp, ChevronRight } from "lucide-react";
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

// ── Queue definitions ────────────────────────────────────────────────────────

type QueueKey = "dropping" | "missed" | "unmeasured" | "overlap" | "promote" | "unclassified";

const QUEUE_META: Record<QueueKey, { label: string; hint: string }> = {
  dropping: { label: "Dropping", hint: "Score is falling — the lesson is losing its grip." },
  missed: { label: "Missed", hint: "Surfaced but did not catch — a known miss." },
  unmeasured: { label: "Unmeasured", hint: "No outcomes recorded. Nothing links this lesson to a check." },
  overlap: { label: "Overlap", hint: "Near-duplicates from the same source — keep one, retire the rest into it." },
  promote: { label: "Promote", hint: "Eligible for org-wide reach — distinct projects and users back it." },
  unclassified: { label: "Unclassified", hint: "No lesson class assigned yet." },
};

const QUEUE_ORDER: QueueKey[] = ["dropping", "missed", "unmeasured", "overlap", "promote", "unclassified"];

function classifyRow(row: LessonListRow): QueueKey[] {
  const queues: QueueKey[] = [];
  if (row.effectiveness?.trend === "dropping") queues.push("dropping");
  if (row.caught_state === "missed" || row.caught_state === "mixed") queues.push("missed");
  if (!row.caught_state || row.caught_state === "unknown" || row.effectiveness?.trend === "unmeasured") {
    queues.push("unmeasured");
  }
  if (row.eligibility?.state === "eligible" && row.reach !== "org") queues.push("promote");
  if (!row.lesson_class || row.lesson_class === "unclassified") queues.push("unclassified");
  return queues;
}

/** Overlap: lessons sharing the same originating item or source — a client-side heuristic. */
function findOverlapIds(rows: LessonListRow[]): Set<string> {
  const bySource = new Map<string, string[]>();
  for (const r of rows) {
    const key = r.item_id || r.source;
    if (!key) continue;
    const arr = bySource.get(key) ?? [];
    arr.push(r.id);
    bySource.set(key, arr);
  }
  const ids = new Set<string>();
  for (const group of bySource.values()) {
    if (group.length > 1) group.forEach((id) => ids.add(id));
  }
  return ids;
}

// ── Main export ──────────────────────────────────────────────────────────────

/** Published catalog. Memory review is the candidate inbox — different empty, different job. */
export function LessonsView() {
  const { id } = useParams();
  if (id) return <LessonDetailPage id={id} />;
  return <LessonListPage />;
}

// ── List page (S9: queue-based upkeep) ───────────────────────────────────────

function LessonListPage() {
  const { activeId } = useProjectCtx();
  const catalogQ = useLessons(activeId);
  const { data, isLoading, isError, refetch } = catalogQ;
  const loading = isLoading;
  const failed = isError;
  const all = data?.results ?? [];
  const published = data?.total ?? 0;
  const hasMore = data?.has_more ?? false;

  const [activeQueue, setActiveQueue] = React.useState<QueueKey>("dropping");
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [focusedIdx, setFocusedIdx] = React.useState(0);

  // Partition rows into queues.
  const overlapIds = React.useMemo(() => findOverlapIds(all), [all]);
  const queueRows = React.useMemo(() => {
    const map: Record<QueueKey, LessonListRow[]> = {
      dropping: [], missed: [], unmeasured: [], overlap: [], promote: [], unclassified: [],
    };
    for (const row of all) {
      const qs = classifyRow(row);
      for (const q of qs) map[q].push(row);
      if (overlapIds.has(row.id) && !map.overlap.includes(row)) map.overlap.push(row);
    }
    return map;
  }, [all, overlapIds]);

  const rows = queueRows[activeQueue];
  const queueCounts = React.useMemo(
    () => QUEUE_ORDER.map((k) => ({ key: k, count: queueRows[k].length })),
    [queueRows],
  );

  // Clamp focus when queue changes.
  React.useEffect(() => { setFocusedIdx(0); }, [activeQueue]);

  // Keyboard navigation: J/K move, X select, Enter open detail.
  const navigate = useNavigate();
  React.useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.key === "j" || e.key === "J") {
        e.preventDefault();
        setFocusedIdx((i) => Math.min(i + 1, rows.length - 1));
      } else if (e.key === "k" || e.key === "K") {
        e.preventDefault();
        setFocusedIdx((i) => Math.max(i - 1, 0));
      } else if (e.key === "x" || e.key === "X") {
        e.preventDefault();
        const id = rows[focusedIdx]?.id;
        if (id) {
          setSelected((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id); else next.add(id);
            return next;
          });
        }
      } else if (e.key === "Enter") {
        e.preventDefault();
        const id = rows[focusedIdx]?.id;
        if (id) navigate(id);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [rows, focusedIdx, navigate]);

  const empty = !loading && !failed && published === 0;
  const allUnmeasured = !empty && all.length > 0 && queueRows.unmeasured.length === all.length;

  const header = (
    <PlaceHeader
      viewName="Lessons"
      purpose="Published memory, scored against whether it is still catching anything. Unpublished candidates live in Memory — this is the catalog of what you have already stood behind."
      action={
        <div className="flex items-center gap-3 font-mono text-[10.5px] text-faint">
          {loading ? (
            <>
              <span className="h-3 w-16 animate-pulse rounded bg-surface-3" />
              <span className="h-3 w-20 animate-pulse rounded bg-surface-3" />
            </>
          ) : (
            <>
              <span>{published} PUBLISHED</span>
              <span>
                {queueRows.unmeasured.length} UNMEASURED{hasMore ? " THIS PAGE" : ""}
              </span>
              <span>
                {queueRows.dropping.length} DROPPING{hasMore ? " THIS PAGE" : ""}
              </span>
            </>
          )}
        </div>
      }
    />
  );

  if (loading) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        {header}
        <div className="min-h-0 flex-1 overflow-y-auto">
          <LessonsListSkeleton />
        </div>
      </div>
    );
  }

  if (failed || !data) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        {header}
        <PlannerError message="The lesson catalog could not be loaded." onRetry={() => refetch()} />
      </div>
    );
  }

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const selectAll = () => {
    if (selected.size === rows.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(rows.map((r) => r.id)));
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {header}

      {/* Queue picker */}
      <div className="flex flex-none flex-col gap-1.5 border-b border-line px-5 py-2.5">
        <div className="flex flex-wrap items-center gap-1.5">
          {queueCounts.map(({ key, count }) => (
            <button
              key={key}
              type="button"
              onClick={() => setActiveQueue(key)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md border px-2 py-1 font-mono text-[10.5px] uppercase tracking-wide transition-colors",
                activeQueue === key
                  ? "border-line-hover bg-surface-3 text-fg"
                  : "border-line-2 text-faint hover:border-line-hover hover:text-muted",
              )}
            >
              {QUEUE_META[key].label}
              <span className={cn(
                "rounded px-1 py-0.5 text-[9px]",
                activeQueue === key ? "bg-surface-2 text-muted" : "bg-surface-2/50 text-faint",
              )}>
                {count}{hasMore ? "+" : ""}
              </span>
            </button>
          ))}
        </div>
        <p className="text-[11.5px] text-faint">{QUEUE_META[activeQueue].hint}</p>
      </div>

      {/* Bulk action bar */}
      {selected.size > 0 && (
        <div className="flex flex-none items-center gap-3 border-b border-line bg-surface-2 px-5 py-2">
          <span className="font-mono text-[10.5px] text-muted">{selected.size} selected</span>
          <button
            type="button"
            onClick={() => setSelected(new Set())}
            className="rounded-md border border-line px-1.5 py-0.5 font-mono text-[9.5px] text-faint hover:text-muted"
          >
            Clear
          </button>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex max-w-3xl flex-col gap-2.5 p-5">
          {empty ? (
            <EmptyCatalog />
          ) : rows.length === 0 ? (
            <div className="py-16 text-center text-[13px] text-muted">
              Nothing in this queue. Pick another above, or search the full catalog.
            </div>
          ) : (
            <>
              {allUnmeasured && activeQueue === "unmeasured" && (
                <div className="rounded-[10px] border border-[#3a2f1a] bg-[rgba(224,179,74,0.08)] px-3.5 py-2.5 text-[12.5px] leading-relaxed text-[#e0b34a]">
                  {hasMore
                    ? `At least ${queueRows.unmeasured.length} of this page of published lessons have no outcomes yet.`
                    : `${published} published lesson${published === 1 ? " has" : "s have"} no outcomes yet.`}{" "}
                  That is <span className="font-semibold">unknown</span>, not effective — nothing has
                  caught or missed since they were published.
                </div>
              )}

              {/* Select-all checkbox */}
              <div className="flex items-center gap-2 px-1">
                <input
                  type="checkbox"
                  checked={selected.size === rows.length && rows.length > 0}
                  onChange={selectAll}
                  className="h-3.5 w-3.5 rounded border-line-2 accent-accent"
                  aria-label="Select all in this queue"
                />
                <span className="font-mono text-[10px] uppercase tracking-wide text-faint">
                  {rows.length} lesson{rows.length !== 1 ? "s" : ""}
                  {hasMore ? " on this page" : ""}
                </span>
                <span className="ml-auto font-mono text-[9.5px] text-faint">
                  J/K move · X select · Enter open
                </span>
              </div>

              {rows.map((row, i) => (
                <LessonQueueRow
                  key={row.id}
                  row={row}
                  focused={i === focusedIdx}
                  selected={selected.has(row.id)}
                  onToggleSelect={() => toggleSelect(row.id)}
                  onOpen={() => navigate(row.id)}
                />
              ))}

              {hasMore && (
                <p className="py-2 text-center text-[12px] text-faint">
                  More lessons exist beyond this page — counts above are this page, not the rest of
                  the catalog.
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Queue row ────────────────────────────────────────────────────────────────

function LessonQueueRow({
  row,
  focused,
  selected,
  onToggleSelect,
  onOpen,
}: {
  row: LessonListRow;
  focused: boolean;
  selected: boolean;
  onToggleSelect: () => void;
  onOpen: () => void;
}) {
  const score = row.effectiveness?.score;
  const trend = row.effectiveness?.trend ?? "unmeasured";

  return (
    <div
      className={cn(
        "flex items-start gap-2.5 rounded-[10px] border px-3 py-2.5 transition-colors",
        focused ? "border-line-hover bg-surface-2" : "border-line-2 bg-surface-2/60",
        selected && "border-[rgba(167,139,250,0.35)] bg-[rgba(167,139,250,0.04)]",
      )}
    >
      <input
        type="checkbox"
        checked={selected}
        onChange={(e) => { e.stopPropagation(); onToggleSelect(); }}
        onClick={(e) => e.stopPropagation()}
        className="mt-1 h-3.5 w-3.5 shrink-0 rounded border-line-2 accent-accent"
        aria-label={`Select ${row.id}`}
      />
      <button
        type="button"
        onClick={onOpen}
        className="min-w-0 flex-1 text-left"
      >
        <p className="line-clamp-2 text-[13px] leading-relaxed text-ink">{row.text}</p>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <span className="inline-flex items-center gap-1 font-mono text-[10.5px] text-faint">
            <span>{score == null ? "—" : score.toFixed(1)}</span>
            <TrendGlyph trend={trend} />
          </span>
          <CaughtChip state={row.caught_state} />
          <ClassChip lessonClass={row.lesson_class} suggested={row.suggested_class} />
          <span className="font-mono text-[9.5px] text-faint">{row.age_state}</span>
          <span className="font-mono text-[9px] text-faint">{row.id}</span>
        </div>
      </button>
      <ChevronRight size={14} className="mt-1 shrink-0 text-faint" />
    </div>
  );
}

// ── Empty state ──────────────────────────────────────────────────────────────

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

// ── Shared chips ─────────────────────────────────────────────────────────────

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

// ── Detail page ──────────────────────────────────────────────────────────────

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

          {/* S9: last-16 outcome strip */}
          <section className="rounded-[10px] border border-line-2 bg-surface-2 p-3.5">
            <div className="mb-2 font-mono text-[10.5px] uppercase tracking-wide text-faint">
              Last surfaced
            </div>
            <OutcomeStrip history={history} />
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

// ── S9: Outcome strip (last 16) ──────────────────────────────────────────────

function OutcomeStrip({ history }: { history: { at: string; score: number | null; caught_state: string }[] }) {
  const last16 = history.slice(-16);
  if (last16.length === 0) {
    return (
      <p className="text-[12.5px] text-muted">
        No outcomes recorded. Nothing links this lesson to a check, so a hit can&apos;t be told apart from noise.
      </p>
    );
  }
  return (
    <div>
      <div className="flex items-center gap-0.5">
        {last16.map((h, i) => {
          const tone = h.caught_state === "caught"
            ? "bg-st-done"
            : h.caught_state === "missed" || h.caught_state === "mixed"
              ? "bg-st-blocked"
              : "bg-faint";
          return (
            <div
              key={`${h.at}-${i}`}
              title={`${h.at}: ${h.caught_state}`}
              className={cn("h-5 w-2.5 rounded-sm", tone)}
            />
          );
        })}
      </div>
      <div className="mt-1.5 flex items-center gap-3 font-mono text-[9.5px] text-faint">
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-sm bg-st-done" /> caught
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-sm bg-st-blocked" /> missed
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-sm bg-faint" /> no outcome
        </span>
        <span className="ml-auto">{last16.length} of last 16</span>
      </div>
    </div>
  );
}

// ── Provenance ───────────────────────────────────────────────────────────────

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

// ── Cluster section ──────────────────────────────────────────────────────────

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

// ── Outcomes section ─────────────────────────────────────────────────────────

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

// ── History spark ────────────────────────────────────────────────────────────

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

// ── Promote panel ────────────────────────────────────────────────────────────

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
          is not "ineligible."
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

/** Spreading a known miss needs a written acknowledgement — unmeasured does not. */
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
