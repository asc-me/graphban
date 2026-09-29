import { Download, Radio, X } from "lucide-react";
import * as React from "react";

import {
  EventListSkeleton,
  FETCH_FAILED,
  PlannerEmpty,
  PlannerError,
  PlannerFilteredEmpty,
} from "@/components/planner/PlannerStates";
import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { Button } from "@/components/ui/button";
import { useProjectCtx } from "@/features/ProjectContext";
import { useEvents } from "@/lib/queries";
import type { ActivityLensId, ActivityRange, EventsQuery } from "@/lib/types";

import { EventPanel } from "./EventPanel";
import { EventRow } from "./EventRow";
import { FacetBar, type FacetDim } from "./FacetBar";
import { Histogram } from "./Histogram";
import { LensTiles } from "./LensTiles";
import {
  bucketRangeLabel,
  groupEvents,
  RANGE_CHOICES,
  refusalActions,
  toCsv,
  type GroupBy,
} from "./model";

const PAGE = 100;

/**
 * The audit ledger, rebuilt as a lens-and-histogram surface (PRD-47 S10 / GRPH-961).
 *
 * Everything it draws comes from columns on the event record. The two things the design
 * shows that the record does not carry — the field-level diff and the trace id — are
 * labelled seams in {@link EventPanel}, not blanks, and the refusal lens publishes the
 * three actions it actually reads so its count cannot be mistaken for every refusal.
 */
export function ActivityView() {
  const { activeId, active } = useProjectCtx();

  const [lens, setLens] = React.useState<ActivityLensId>("everything");
  const [range, setRange] = React.useState<ActivityRange>("24h");
  const [actor, setActor] = React.useState<string | null>(null);
  const [surface, setSurface] = React.useState<string | null>(null);
  const [targetType, setTargetType] = React.useState<string | null>(null);
  const [targetId, setTargetId] = React.useState<string | null>(null);
  const [bucket, setBucket] = React.useState<number | null>(null);
  const [groupBy, setGroupBy] = React.useState<GroupBy>("time");
  const [limit, setLimit] = React.useState(PAGE);
  const [focused, setFocused] = React.useState(0);
  const [openId, setOpenId] = React.useState<number | null>(null);

  const query: EventsQuery = React.useMemo(
    () => ({ projectId: activeId, limit, lens, range, actor, surface, targetType, targetId, bucket }),
    [activeId, limit, lens, range, actor, surface, targetType, targetId, bucket],
  );
  const { data, isLoading, isError, refetch } = useEvents(query);

  const filtersActive =
    lens !== "everything" ||
    actor !== null ||
    surface !== null ||
    targetType !== null ||
    targetId !== null ||
    bucket !== null;

  const clearFilters = React.useCallback(() => {
    setLens("everything");
    setActor(null);
    setSurface(null);
    setTargetType(null);
    setTargetId(null);
    setBucket(null);
  }, []);

  // A narrower selection must not be paged through with the old page size, and the
  // cursor must not point past the end of a list that just shrank.
  React.useEffect(() => {
    setLimit(PAGE);
    setFocused(0);
  }, [lens, range, actor, surface, targetType, targetId, bucket, groupBy]);

  const rows = data?.results ?? [];
  const refusals = React.useMemo(() => refusalActions(data?.lenses), [data]);
  const groups = React.useMemo(() => groupEvents(rows, groupBy, range), [rows, groupBy, range]);
  const openEvent = React.useMemo(
    () => (openId === null ? null : rows.find((r) => r.id === openId) ?? null),
    [openId, rows],
  );

  // J/K move, Enter opens the panel, Escape closes it.
  React.useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const t = e.target as HTMLElement | null;
      if (t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t instanceof HTMLSelectElement) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "j" || e.key === "J") {
        e.preventDefault();
        setFocused((i) => (rows.length === 0 ? 0 : Math.min(i + 1, rows.length - 1)));
      } else if (e.key === "k" || e.key === "K") {
        e.preventDefault();
        setFocused((i) => Math.max(i - 1, 0));
      } else if (e.key === "Enter") {
        const id = rows[focused]?.id;
        if (id !== undefined) {
          e.preventDefault();
          setOpenId(id);
        }
      } else if (e.key === "Escape") {
        setOpenId(null);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [rows, focused]);

  const listRef = React.useRef<HTMLDivElement>(null);
  const focusedId = rows[focused]?.id;
  React.useEffect(() => {
    if (focusedId === undefined) return;
    listRef.current?.querySelector(`[data-event-id="${focusedId}"]`)?.scrollIntoView({ block: "nearest" });
  }, [focusedId]);

  function exportCsv() {
    if (rows.length === 0) return;
    const blob = new Blob([toCsv(rows, refusals)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `activity-${active?.tag ?? "project"}-${range}${lens === "everything" ? "" : `-${lens}`}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const header = (
    <PlaceHeader
      viewName="Activity"
      purpose="Every accepted mutation, attributed to the person, key and agent behind it — plus the refusals the ledger records."
      action={
        data ? (
          <div className="flex items-center gap-3">
            <span className="text-[11.5px] text-muted" title="Matching this selection, of everything the ledger holds for the projects you can read.">
              <span className="font-mono text-fg">{data.total.toLocaleString()}</span>
              {" "}of <span className="font-mono">{data.ledger_total.toLocaleString()}</span> recorded
            </span>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={exportCsv}
              disabled={rows.length === 0}
              title={
                data.has_more
                  ? `Exports the ${rows.length} rows in this view. ${data.total.toLocaleString()} match — the rest are beyond this page.`
                  : `Exports the ${rows.length} rows in this view.`
              }
            >
              <Download size={13} />
              Export {rows.length} rows
            </Button>
          </div>
        ) : undefined
      }
    />
  );

  // PRD-47 S1: the header stays put through every state, and a failed fetch must not
  // fall through to "No activity yet" — a request that never answered is not a project
  // where nothing happened.
  if (isError && !data) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        {header}
        <PlannerError message={FETCH_FAILED} onRetry={() => void refetch()} />
      </div>
    );
  }
  if (isLoading || !data) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        {header}
        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          <div className="mx-auto max-w-3xl">
            <EventListSkeleton />
          </div>
        </div>
      </div>
    );
  }

  const facetSelection: Record<FacetDim, string | null> = {
    person: actor,
    surface,
    object: targetType,
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {header}

      <div className="flex flex-none flex-col gap-2.5 border-b border-line px-5 py-3">
        <OtlpStrip />
        <LensTiles lenses={data.lenses} active={lens} onPick={setLens} />

        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <label className="flex items-center gap-1.5 text-[11.5px] text-faint">
            Range
            <select
              value={range}
              onChange={(e) => {
                setRange(e.target.value as ActivityRange);
                setBucket(null);
              }}
              className="rounded-md border border-line-2 bg-surface px-2 py-0.5 text-[12px] text-ink focus:border-line-hover focus:outline-none"
            >
              {RANGE_CHOICES.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.label}
                </option>
              ))}
            </select>
          </label>

          <label className="flex items-center gap-1.5 text-[11.5px] text-faint">
            Group by
            <select
              value={groupBy}
              onChange={(e) => setGroupBy(e.target.value as GroupBy)}
              className="rounded-md border border-line-2 bg-surface px-2 py-0.5 text-[12px] text-ink focus:border-line-hover focus:outline-none"
            >
              <option value="time">Time</option>
              <option value="target">Target</option>
            </select>
          </label>

          <div className="flex flex-wrap items-center gap-1.5">
            {bucket !== null && data.histogram.buckets[bucket] && (
              <Chip
                label={`Bar ${bucket + 1}: ${bucketRangeLabel(data.histogram.buckets[bucket])}`}
                onClear={() => setBucket(null)}
              />
            )}
            {actor !== null && (
              <Chip
                label={`person: ${facetLabel(data.facets.person.values, actor)}`}
                onClear={() => setActor(null)}
              />
            )}
            {surface !== null && <Chip label={`surface: ${surface || "(unset)"}`} onClear={() => setSurface(null)} />}
            {targetType !== null && (
              <Chip label={`object: ${targetType || "untyped"}`} onClear={() => setTargetType(null)} />
            )}
            {targetId !== null && <Chip label={`target: ${targetId}`} onClear={() => setTargetId(null)} />}
            {filtersActive && (
              <button
                type="button"
                onClick={clearFilters}
                className="rounded-md border border-line px-1.5 py-0.5 text-[10.5px] text-faint transition-colors hover:border-line-hover hover:text-muted"
              >
                Clear all
              </button>
            )}
          </div>

          <span className="ml-auto text-[10.5px] text-faint-2">J/K move · Enter open · Esc close</span>
        </div>

        <Histogram histogram={data.histogram} selected={bucket} onPick={setBucket} />
        <FacetBar
          facets={data.facets}
          selected={facetSelection}
          onToggle={(dim, value) => {
            if (dim === "person") setActor((v) => (v === value ? null : value));
            else if (dim === "surface") setSurface((v) => (v === value ? null : value));
            else setTargetType((v) => (v === value ? null : value));
            setTargetId(null);
          }}
        />
      </div>

      <div className="flex min-h-0 flex-1">
        <div ref={listRef} className="min-w-0 flex-1 overflow-y-auto p-5">
          {rows.length === 0 ? (
            data.ledger_total === 0 ? (
              <PlannerEmpty
                title="No activity yet"
                description="Every accepted or rejected mutation lands here, attributed to the person, key and agent behind it. Nothing has ever been recorded for the projects you can read — this is not a filter result."
              />
            ) : (
              <PlannerFilteredEmpty
                message={`Nothing in this selection. The ledger holds ${data.ledger_total.toLocaleString()} events overall, so this is the range or a filter, not an empty project.`}
                onClear={clearFilters}
              />
            )
          ) : (
            <div className="mx-auto flex max-w-3xl flex-col gap-3">
              {groups.map((g) => (
                <section key={g.key} className="flex flex-col gap-1.5">
                  <h2 className="px-1 text-[11.5px] text-faint">
                    {g.label}
                    <span className="ml-1.5 font-mono text-[10px] text-faint-2">{g.rows.length}</span>
                  </h2>
                  {g.rows.map((e) => {
                    const idx = rows.indexOf(e);
                    return (
                      <EventRow
                        key={e.id}
                        event={e}
                        refusals={refusals}
                        focused={idx === focused}
                        onOpen={() => {
                          setFocused(idx);
                          setOpenId(e.id);
                        }}
                      />
                    );
                  })}
                </section>
              ))}

              {data.has_more && (
                <div className="flex flex-col items-center gap-1.5 py-2">
                  <p className="text-[12px] text-faint">
                    {rows.length.toLocaleString()} of {data.total.toLocaleString()} matching events
                    shown.
                  </p>
                  <Button type="button" variant="outline" size="sm" onClick={() => setLimit((l) => l + PAGE)}>
                    Show {PAGE} more
                  </Button>
                </div>
              )}
            </div>
          )}
        </div>

        {openEvent && (
          <EventPanel
            event={openEvent}
            refusals={refusals}
            onClose={() => setOpenId(null)}
            onPivotTarget={() => {
              setTargetType(openEvent.target_type);
              setTargetId(openEvent.target_id);
              setBucket(null);
            }}
            onPivotActor={() => {
              setActor(openEvent.actor_id);
              setBucket(null);
            }}
          />
        )}
      </div>
    </div>
  );
}

/** The facet's own label for a value, falling back to the raw value. */
function facetLabel(values: { value: string; label: string }[], value: string): string {
  return values.find((v) => v.value === value)?.label ?? (value || "(unnamed)");
}

function Chip({ label, onClear }: { label: string; onClear: () => void }) {
  return (
    <span className="inline-flex max-w-[22rem] items-center gap-1.5 rounded-md border border-line-hover bg-surface-3 px-2 py-0.5 text-[11px] text-fg">
      <span className="truncate">{label}</span>
      <button
        type="button"
        onClick={onClear}
        aria-label={`Clear ${label}`}
        className="flex-none rounded p-px text-faint transition-colors hover:text-st-blocked"
      >
        <X size={11} />
      </button>
    </span>
  );
}

/**
 * Where this ledger goes when it leaves the box — which today is nowhere.
 *
 * Deliberately not a link. The OTLP settings panel is S15 and does not exist; Settings
 * falls through to the AI Providers pane on an unknown path, so a link there would land
 * on a wrong pane that looks like the right one. The `soon` marker is the same idiom the
 * nav uses for a destination the design names but nothing serves yet.
 */
function OtlpStrip() {
  return (
    <div className="flex items-center gap-2 rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2">
      <Radio size={13} className="flex-none text-faint" aria-hidden />
      <p className="min-w-0 flex-1 text-[11.5px] leading-snug text-muted">
        Log export is not built. Nothing here is being sent to a collector and nothing is
        queued for one, so there is no last batch, no send count and no queue depth to
        report — events stay in this database only.
      </p>
      <span
        className="flex-none rounded border border-line-2 px-1.5 py-px text-[9px] text-faint-2"
        title="The OTLP panel is PRD-47 S15 and is not built yet."
      >
        soon
      </span>
    </div>
  );
}
