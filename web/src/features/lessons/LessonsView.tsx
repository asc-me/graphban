import { ArrowLeft, ArrowDown, ArrowRight, ArrowUp } from "lucide-react";
import * as React from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { cn } from "@/lib/cn";
import { useProjectCtx } from "@/features/ProjectContext";
import {
  useLesson,
  useLessons,
  usePromoteOrgLesson,
  useRecordLessonOutcome,
} from "@/lib/queries";
import type {
  Eligibility,
  LessonDetail,
  LessonEnums,
  LessonFilters,
  LessonListRow,
} from "@/lib/types";

/** Published catalog. Memory review is the candidate inbox — different empty, different job. */
export function LessonsView() {
  const { id } = useParams();
  if (id) return <LessonDetailPage id={id} />;
  return <LessonListPage />;
}

function LessonListPage() {
  const { activeId } = useProjectCtx();
  const [filters, setFilters] = React.useState<LessonFilters>({});
  const compact = compactFilters(filters);
  const filtered = Object.keys(compact).length > 0;
  const { data: catalog } = useLessons(activeId);
  const { data, isLoading } = useLessons(activeId, filtered ? compact : undefined);

  if (isLoading || !data) {
    return <div className="flex h-full items-center justify-center text-[13px] text-muted">Loading…</div>;
  }

  const enums = data.enums ?? catalog?.enums;
  const all = catalog?.results ?? data.results;
  const published = catalog?.total ?? data.total;
  const unmeasured = all.filter((r) => r.caught_state === "unknown").length;
  const dropping = all.filter((r) => r.effectiveness?.trend === "dropping").length;
  const empty = published === 0;
  const allUnmeasured = !empty && all.length > 0 && unmeasured === all.length;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-none items-center gap-4 border-b border-line px-5 py-4">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight">Lessons</h1>
          <p className="mt-0.5 text-[12.5px] text-muted">
            Published memory, scored against whether it is still catching anything. Candidates stay in
            Memory until you publish them.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-3 font-mono text-[10.5px] text-faint">
          <span>{published} PUBLISHED</span>
          <span>{unmeasured} UNMEASURED</span>
          <span>{dropping} DROPPING</span>
        </div>
      </div>

      {enums && (
        <FilterBar enums={enums} filters={compact} onChange={setFilters} />
      )}

      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        <div className="mx-auto flex max-w-3xl flex-col gap-2.5">
          {empty ? (
            <EmptyCatalog />
          ) : (
            <>
              {allUnmeasured && (
                <div className="rounded-[10px] border border-[#3a2f1a] bg-[rgba(224,179,74,0.08)] px-3.5 py-2.5 text-[12.5px] leading-relaxed text-[#e0b34a]">
                  {published} published lesson{published === 1 ? " has" : "s have"} no outcomes yet.
                  That is <span className="font-semibold">unknown</span>, not effective — nothing has
                  caught or missed since they were published.
                </div>
              )}
              {data.results.length === 0 ? (
                <div className="py-16 text-center text-[13px] text-muted">
                  No lessons match these filters.
                </div>
              ) : (
                data.results.map((row) => (
                  <LessonRow key={row.id} row={row} />
                ))
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function EmptyCatalog() {
  return (
    <div className="py-16 text-center text-[13px] leading-relaxed text-muted">
      No published lessons in this project. Agent-written notes queue in{" "}
      <Link to="/memory-review" className="text-fg underline decoration-line-hover underline-offset-2 hover:text-ink">
        Memory
      </Link>{" "}
      until you publish them. This page is the catalog of what you have already stood behind — it is
      not a scoreboard, and an empty catalog is not a high score.
    </div>
  );
}

function FilterBar({
  enums,
  filters,
  onChange,
}: {
  enums: LessonEnums;
  filters: LessonFilters;
  onChange: (next: LessonFilters) => void;
}) {
  const toggle = (dim: keyof LessonFilters, value: string) => {
    onChange({
      ...filters,
      [dim]: filters[dim] === value ? undefined : value,
    });
  };
  const unclassified = enums.unclassified_filter || "unclassified";
  return (
    <div className="flex flex-none flex-col gap-1.5 border-b border-line px-5 py-2.5">
      <ChipRow>
        {(enums.trends ?? []).map((v) => (
          <FilterChip
            key={v}
            label={trendLabel(v)}
            active={filters.trend === v}
            onClick={() => toggle("trend", v)}
          />
        ))}
      </ChipRow>
      <ChipRow>
        {(enums.caught_states ?? []).map((v) => (
          <FilterChip
            key={v}
            label={caughtLabel(v)}
            active={filters.caught_state === v}
            onClick={() => toggle("caught_state", v)}
          />
        ))}
      </ChipRow>
      <ChipRow>
        {(enums.eligibilities ?? []).map((v) => (
          <FilterChip
            key={v}
            label={eligLabel(v)}
            active={filters.eligibility === v}
            onClick={() => toggle("eligibility", v)}
          />
        ))}
      </ChipRow>
      <ChipRow>
        <FilterChip
          label="Unclassified"
          active={filters.lesson_class === unclassified}
          onClick={() => toggle("lesson_class", unclassified)}
        />
        {(enums.lesson_classes ?? []).map((v) => (
          <FilterChip
            key={v}
            label={v}
            active={filters.lesson_class === v}
            onClick={() => toggle("lesson_class", v)}
          />
        ))}
      </ChipRow>
    </div>
  );
}

function ChipRow({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-wrap items-center gap-1.5">{children}</div>;
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

function LessonRow({ row }: { row: LessonListRow }) {
  const { projects, activeId } = useProjectCtx();
  const originTag =
    row.project_id && row.project_id !== activeId
      ? projects.find((p) => p.id === row.project_id)?.tag
      : null;
  return (
    <Link
      to={row.id}
      className="block rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3 transition-colors hover:border-line-hover"
    >
      <p className="line-clamp-2 text-[13px] leading-relaxed text-ink">{row.text}</p>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
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
        <EligChip eligibility={row.eligibility} />
      </div>
    </Link>
  );
}

function ScoreChip({ row }: { row: LessonListRow }) {
  // A missing effectiveness field is unmeasured, never a defaulted 1.0.
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

function EligChip({ eligibility }: { eligibility: Eligibility | undefined }) {
  const state = eligibility?.state || "unverifiable";
  const tone =
    state === "eligible" ? "done" : state === "promoted" ? "accent" : state === "unverifiable" ? "review" : "muted";
  return (
    <Chip tone={tone} title={eligibility?.reason}>
      {state}
    </Chip>
  );
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
  const { data, isLoading, isError } = useLesson(activeId, id);

  if (isLoading) {
    return <div className="flex h-full items-center justify-center text-[13px] text-muted">Loading…</div>;
  }
  if (isError || !data) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 text-[13px] text-muted">
        <p>Lesson not found.</p>
        <button type="button" onClick={() => navigate("..")} className="text-fg underline">
          Back to Lessons
        </button>
      </div>
    );
  }

  return <LessonDetailBody lesson={data} />;
}

function LessonDetailBody({ lesson }: { lesson: LessonDetail }) {
  const navigate = useNavigate();
  const score = lesson.effectiveness?.score;
  const trend = lesson.effectiveness?.trend ?? "unmeasured";
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
  if (scan === "cluster_scope_unmeasured") {
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
    </div>
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
  const reasonText = elig?.reason || "";

  if (lesson.reach === "org") {
    const overridden = lesson.transferability === "overridden";
    return (
      <section className="rounded-[10px] border border-line-2 bg-surface-2 p-3.5">
        <div className="font-mono text-[10.5px] uppercase tracking-wide text-faint">Org promotion</div>
        <div className="mt-2">
          <Chip tone="accent">{overridden ? "org (overridden)" : "org · evidenced"}</Chip>
        </div>
        <p className="mt-2 text-[12.5px] text-muted">
          {overridden
            ? "Promoted with a written override — the independence formula did not pass."
            : "Already org-reach. Visible on sibling projects via retrieval widening."}
        </p>
      </section>
    );
  }

  const failing = isFailing(lesson);
  const state = elig?.state;
  const unverifiable = state === "unverifiable";
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
    </section>
  );
}

/** Spreading a known miss needs a written acknowledgement — unmeasured does not. */
function isFailing(lesson: LessonDetail): boolean {
  const trend = lesson.effectiveness?.trend;
  const caught = lesson.caught_state;
  if (trend === "dropping") return true;
  if (caught === "missed" || caught === "mixed") return true;
  return (lesson.outcomes ?? []).some((o) => o.kind === "contradicted");
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

function trendLabel(v: string): string {
  if (v === "dropping") return "Dropping";
  if (v === "unmeasured") return "Unmeasured";
  return v;
}

function caughtLabel(v: string): string {
  if (v === "unknown") return "Unknown outcomes";
  return v;
}

function eligLabel(v: string): string {
  if (v === "eligible") return "Eligible for org";
  return v;
}

function compactFilters(f: LessonFilters): LessonFilters {
  const out: LessonFilters = {};
  if (f.trend) out.trend = f.trend;
  if (f.caught_state) out.caught_state = f.caught_state;
  if (f.eligibility) out.eligibility = f.eligibility;
  if (f.lesson_class) out.lesson_class = f.lesson_class;
  return out;
}
