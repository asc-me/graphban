import { useMemo, useState } from "react";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { Preferences } from "@/features/harness/Preferences";
import { PlannerError, TableSkeleton } from "@/components/planner/PlannerStates";
import { CapacityPanel } from "@/features/harness/CapacityPanel";
import { Recommendations, defaultVisibleCount } from "@/features/harness/Recommendations";
import { GuidanceTab } from "@/features/harness/GuidanceTab";
import { PerformanceGrid } from "@/features/harness/PerformanceGrid";
import { EffortCurve } from "@/features/harness/EffortCurve";
import { SurfaceReport } from "@/features/harness/Surface";
import { accentMap, pairKey } from "@/features/harness/look";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { useFleet, useHarness, useHarnessCards, useHarnessGuidance } from "@/lib/queries";
import type { HarnessReport, HarnessReviewCell } from "@/lib/types";

type TabId = "performance" | "guidance" | "changes" | "capacity";

const TABS: { id: TabId; label: string }[] = [
  { id: "performance", label: "Performance" },
  { id: "guidance", label: "Guidance for gbfleet" },
  { id: "changes", label: "Changes" },
  { id: "capacity", label: "Capacity" },
];

const PURPOSE = (windowDays: number | string, floor: number | string) =>
  `How each model has turned out, per function and effort band, over the last ${windowDays} days. A rate under ${floor} finished attempts is shown grey because it is not yet a measurement — and gbfleet is told the same.`;

/**
 * PRD-47 S12 — four tabs (Capacity is PRD-62 D8). Performance is the harness : model ×
 * function grid and the effort curve; Guidance shows the routing table gbfleet is served,
 * graded on the server; Changes holds the recommendation cards (the probe panel is retired, GRPH-P98).
 * Nothing on any tab applies anything — that is what the cards themselves say.
 */
export function HarnessView() {
  const { activeId, active } = useProjectCtx();
  const scope = active?.tag || active?.name || activeId;
  const [versions, setVersions] = useState<"current" | "all">("current");
  const [tab, setTab] = useState<TabId>("performance");
  const harnessQ = useHarness(activeId, { versions });
  const { data, isLoading } = harnessQ;
  const { data: guidance } = useHarnessGuidance(activeId);
  // SERVED is a status claim: only show it when routing or fleet_status text was served.
  const guidanceServed = !!guidance && (guidance.routing.length > 0 || !!guidance.served_text || !!guidance.fleet_status_text);
  const { data: cards } = useHarnessCards(activeId);
  const { data: fleetData, refetch: refetchFleet } = useFleet(activeId);
  // One colour per harness : model on every tab — the grid, the curve and the guidance picks.
  const accents = useMemo(
    () => accentMap([
      ...(data?.cells ?? []).map((c) => pairKey(c.key.vendor, c.key.model)),
      ...(guidance?.routing ?? []).map((r) => r.pick),
    ]),
    [data?.cells, guidance?.routing],
  );

  if (harnessQ.isError && !data) {
    return (
      <div className="flex h-full min-h-0 flex-col" data-testid="harness-error">
        <PlaceHeader viewName="Harness" purpose="How each model has turned out, per function and effort band." />
        <PlannerError
          message="The catalog has not been served. That is not an empty matrix."
          onRetry={() => void harnessQ.refetch()}
        />
      </div>
    );
  }

  if (isLoading || !data) {
    return (
      <div className="flex h-full min-h-0 flex-col" data-testid="harness-loading">
        <PlaceHeader viewName="Harness" purpose="How each model has turned out, per function and effort band." />
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          <TableSkeleton rows={8} columns={5} />
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="harness-view">
      <PlaceHeader
        viewName="Harness"
        purpose={PURPOSE(data.window_days, data.floor)}
        action={
          <div className="flex items-center gap-2">
            <label className="font-mono text-[10.5px] text-faint" htmlFor="harness-versions">
              VERSIONS
            </label>
            <select
              id="harness-versions"
              aria-label="Binary versions"
              data-testid="harness-versions"
              className="h-[30px] rounded-[8px] border border-control bg-surface-2 px-2 text-[12px]"
              value={versions}
              onChange={(e) => setVersions(e.target.value as "current" | "all")}
            >
              <option value="current">Current only</option>
              <option value="all">Every version</option>
            </select>
          </div>
        }
      />

      <div role="tablist" aria-label="Harness" className="flex gap-0.5 border-b border-line-2 px-6 pt-3.5" data-testid="harness-tabs">
        {TABS.map((t) => {
          const on = tab === t.id;
          return (
            <button
              key={t.id}
              type="button"
              role="tab"
              data-testid="harness-tab"
              data-tab={t.id}
              aria-selected={on}
              onClick={() => setTab(t.id)}
              className={cn(
                "-mb-px inline-flex items-center gap-[7px] border-b-2 px-3.5 py-[9px] text-[13px] transition-colors hover:text-fg",
                on ? "border-accent text-fg" : "border-transparent text-muted",
              )}
            >
              {t.label}
              {t.id === "guidance" && guidanceServed && (
                <span className="rounded-[4px] border border-purple/35 px-[5px] font-mono text-meta text-purple" data-testid="harness-tab-badge">
                  SERVED
                </span>
              )}
              {t.id === "changes" && cards && (
                <span
                  className="rounded-[4px] border border-line-2 px-[5px] font-mono text-meta text-muted"
                  title={`${cards.cards.length} cards in total`}
                  data-testid="harness-tab-badge"
                >
                  {defaultVisibleCount(cards.cards)}
                </span>
              )}
            </button>
          );
        })}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-12 pt-[18px]">
        {tab === "performance" && (
          <PerformanceTab
            data={data}
            fleetData={fleetData}
            routing={guidance?.routing ?? []}
            scope={scope}
            activeId={activeId}
            onRefetchFleet={() => { void refetchFleet(); }}
          />
        )}
        {tab === "guidance" && (
          <GuidanceTab
            guidance={guidance}
            order={data.capability_set?.leaves ?? []}
            accent={(pick) => accents.get(pick) ?? "#8b949e"}
          />
        )}
        {tab === "changes" && <ChangesTab activeId={activeId} />}
        {tab === "capacity" && (
          <div className="max-w-6xl">
            <CapacityPanel projectId={activeId} />
          </div>
        )}
      </div>
    </div>
  );
}

function PerformanceTab({
  data,
  fleetData,
  routing,
  scope,
  activeId,
  onRefetchFleet,
}: {
  data: HarnessReport;
  fleetData?: import("@/lib/types").FleetOverview;
  routing: import("@/lib/types").HarnessGuidance["routing"];
  scope: string;
  activeId: string;
  onRefetchFleet: () => void;
}) {
  if (data.cells.length === 0 && !(data.unavailable ?? []).length) {
    return (
      <div className="flex flex-col gap-6">
        <div className="mx-auto mt-14 flex max-w-[460px] flex-col items-center gap-3 text-center" data-testid="harness-empty">
          <div className="flex size-10 items-center justify-center rounded-[12px] border border-dashed border-line">
            <span className="size-2 rounded-[3px] bg-purple" />
          </div>
          <p className="text-[13px] leading-[1.65] text-muted">
            Nothing measured yet. A cell appears here once a delegation finishes — one row per
            vendor, model, capability and size band.
          </p>
          <p className="text-[12px] leading-[1.6] text-faint">
            Until then, gbfleet gets no guidance and resolves every tier by matrix order and policy alone.
          </p>
        </div>
        <div className="mx-auto w-full max-w-4xl">
          <SurfaceReport surface={data.surface} />
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <PerformanceGrid data={data} routing={routing} />
      <EffortCurve cells={data.cells} floor={data.floor} />
      {(data.review_cells ?? []).length > 0 && <ReviewQuality cells={data.review_cells ?? []} floor={data.floor} />}
      <SurfaceReport surface={data.surface} />
      <div className="mt-3">
        <Preferences
          projectId={activeId}
          scope={scope}
          profile={fleetData?.profile ?? null}
          policy={fleetData?.policy ?? null}
          onSaved={onRefetchFleet}
        />
      </div>
    </div>
  );
}

function ChangesTab({ activeId }: { activeId: string }) {
  return (
    <div className="flex max-w-[900px] flex-col gap-3">
      <p className="text-body leading-[1.6] text-muted">
        Recommended changes are drafts. Accepting one records that you have seen it — R1 and R2
        hand you evidence text for a commit, R3 and R4 send you to the profile or policy screen.
        This page changes nothing.
      </p>
      <Recommendations projectId={activeId} />
    </div>
  );
}

/**
 * Review competence (F1–F3) is measured on CHECKED VERDICTS, not finished builds, so it is a
 * different count and stays out of the build grid. One line per reviewer : model.
 */
function ReviewQuality({ cells, floor }: { cells: HarnessReviewCell[]; floor: number }) {
  return (
    <section className="rounded-[12px] border border-line-2 bg-surface px-4 py-3.5" data-testid="harness-review-quality">
      <div className="mb-2.5 flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
        <h2 className="text-lead font-semibold">Verdict quality</h2>
        <span className="text-[12px] text-faint">Review competence, counted on checked verdicts — not on builds.</span>
      </div>
      <div className="flex flex-col divide-y divide-line-2/60">
        {cells.map((cell) => (
          <div
            key={`f:${cell.key.vendor}:${cell.key.model}:${cell.key.capability}:${cell.key.size_band}`}
            data-testid="harness-review-cell"
            className="flex flex-wrap items-baseline gap-x-4 gap-y-1 py-2 font-mono text-small"
          >
            <span className="text-fg">{cell.key.vendor}{cell.key.model ? `:${cell.key.model}` : ""}</span>
            <span className="text-faint">{cell.key.capability} · {cell.key.size_band}</span>
            <span data-testid="harness-f1" className="text-muted">
              F1 {cell.f1.rate === null ? "—" : `${Math.round(cell.f1.rate * 100)}%`} n={cell.f1.n}
            </span>
            <span data-testid="harness-f2" className="text-muted">
              F2 miss {cell.f2.miss}/{cell.f2.n} unconfirmed {cell.f2.miss_unconfirmed}
            </span>
            <span data-testid="harness-f3" className="text-muted">
              F3 unclassified {cell.f3.unclassified === null ? "—" : `${Math.round(cell.f3.unclassified * 100)}%`}
            </span>
            <span data-testid="harness-f2-label" className="rounded-full border border-line-2 px-2 text-[10px] text-faint">
              {cell.f2.label}
            </span>
            <span className="ml-auto text-faint">{cell.checked} checked verdicts</span>
            {cell.below_floor && (
              <span data-testid="harness-review-below-floor" className="w-full rounded-full text-[10px] text-faint">
                below the floor — {cell.checked} of {floor} checked verdicts
              </span>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
