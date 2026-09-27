import { AlertTriangle, Scale } from "lucide-react";

import { Preferences } from "@/features/harness/Preferences";
import { useFleet } from "@/lib/queries";
import type { HarnessCell, HarnessCost, HarnessPoint, HarnessReport, HarnessReviewCell } from "@/lib/types";

const SIZE_ORDER = ["S", "M", "L"];

/**
 * PRD-47 S12 — Performance tab.
 *
 * The grid, the effort bands, the below-floor grey, the cell detail with its weekly trend,
 * and the effort curve — signed-off rate as items get bigger, above-floor cells only.
 */
export function PerformanceTab({ data, versions, onVersionsChange }: {
  data: HarnessReport;
  versions: "current" | "all";
  onVersionsChange: (v: "current" | "all") => void;
}) {
  const { data: fleetData, refetch: refetchFleet } = useFleet(data.project_id);
  const scope = data.project_id ?? "";

  void versions;
  void onVersionsChange;

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-3 p-5">
      {data.cells.length === 0 && !(data.unavailable ?? []).length ? (
        <div className="mx-auto mt-16 max-w-md text-center text-[13px] text-muted">
          Nothing measured yet. A cell appears here once a delegation finishes — one row per
          vendor, model, capability and size band. Until then gbfleet gets no guidance and
          resolves every tier by matrix order and policy alone.
        </div>
      ) : (
        <>
          {data.coverage && data.coverage.attempts > 0 && (
            <div
              data-testid="harness-coverage"
              className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-[12.5px] text-muted"
            >
              Coverage {data.coverage.rate === null ? "—" : `${Math.round(data.coverage.rate * 100)}%`}
              {" — "}
              {data.coverage.with_leaf}/{data.coverage.attempts} attempts tagged a leaf.
              Attempts that match none land in <span className="font-mono">other</span>.
            </div>
          )}
          {data.platform === null && data.platform_reason && (
            <div
              data-testid="harness-no-platform"
              className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-[12.5px] text-muted"
            >
              {data.platform_reason}
            </div>
          )}
          {data.below_floor_count > 0 && (
            <div
              data-testid="harness-floor-note"
              className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-[12.5px] text-muted"
            >
              {data.below_floor_count} of {data.cells.length} cells are below the{" "}
              {data.floor}-attempt floor. Their rates are shown because hiding them would read
              as having none, not as having too few.
            </div>
          )}
          <EffortCurve cells={data.cells} floor={data.floor} />
          {(data.review_cells ?? []).map((cell) => (
            <ReviewRow key={`f:${cell.key.vendor}:${cell.key.model}:${cell.key.capability}`} cell={cell} floor={data.floor} />
          ))}
          {(data.unavailable ?? []).map((row) => (
            <div
              key={`${row.vendor}:${row.model}:${row.capability}:${row.reason}`}
              data-testid="harness-unavailable"
              className="rounded-[10px] border border-dashed border-line-2 bg-surface-2 px-3.5 py-3 text-faint"
            >
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="font-mono text-[12.5px]">
                  {row.vendor}{row.model ? `:${row.model}` : ""}
                </span>
                <span className="font-mono text-[10.5px]">{row.capability}</span>
                <span data-testid="harness-unavailable-reason" className="font-mono text-[10.5px]">
                  {row.label || row.reason}
                </span>
              </div>
              <p className="mt-1 text-[12px]">
                Greyed because it is {row.reason}, not because it is unmeasured.
                Control: <span className="font-mono">{row.control}</span>
                {row.drops ? ` · dropped ${row.drops} times` : ""}
              </p>
            </div>
          ))}
          {data.cells.map((cell) => (
            <CellRow key={cellId(cell)} cell={cell} floor={data.floor} />
          ))}
        </>
      )}

      <div className="mt-6">
        <Preferences
          projectId={data.project_id ?? ""}
          scope={scope}
          profile={fleetData?.profile ?? null}
          policy={fleetData?.policy ?? null}
          onSaved={() => { void refetchFleet(); }}
        />
      </div>
    </div>
  );
}

/**
 * The effort curve — signed-off rate as items get bigger, above-floor cells only.
 *
 * Each point is one cell's rate at one size band. Cells below the floor are excluded:
 * including them would make the curve read as noise, and the floor exists precisely because
 * a rate under it is not yet a measurement.
 */
function EffortCurve({ cells }: { cells: HarnessCell[]; floor: number }) {
  const aboveFloor = cells.filter((c) => !c.below_floor && c.rate !== null && c.kind !== "family");
  if (aboveFloor.length === 0) return null;

  const byBand: Record<string, { total: number; signedOff: number; finished: number; cells: number }> = {};
  for (const band of SIZE_ORDER) {
    byBand[band] = { total: 0, signedOff: 0, finished: 0, cells: 0 };
  }
  for (const c of aboveFloor) {
    const band = c.key.size_band;
    if (!byBand[band]) byBand[band] = { total: 0, signedOff: 0, finished: 0, cells: 0 };
    byBand[band].signedOff += c.signed_off;
    byBand[band].finished += c.finished;
    byBand[band].cells += 1;
    byBand[band].total += c.rate ?? 0;
  }

  const bands = SIZE_ORDER.filter((b) => byBand[b] && byBand[b].cells > 0);
  if (bands.length === 0) return null;

  const maxRate = 1;

  return (
    <div data-testid="harness-effort-curve" className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
      <div className="flex items-baseline gap-2">
        <span className="text-[13px] font-semibold">Effort curve</span>
        <span className="font-mono text-[10.5px] text-faint">
          signed-off rate by size band — above-floor cells only
        </span>
      </div>
      <div className="mt-3 flex items-end gap-4" style={{ height: 80 }}>
        {bands.map((band) => {
          const agg = byBand[band];
          const avgRate = agg.finished > 0 ? agg.signedOff / agg.finished : 0;
          const barH = 4 + Math.round((avgRate / maxRate) * 64);
          return (
            <div key={band} className="flex flex-1 flex-col items-center gap-1">
              <span className="font-mono text-[10px] text-muted">
                {avgRate === 0 ? "—" : `${Math.round(avgRate * 100)}%`}
              </span>
              <div
                data-testid="harness-effort-bar"
                data-band={band}
                className="w-full max-w-[60px] rounded-[3px] bg-st-done"
                style={{ height: barH }}
                title={`${band}: ${agg.signedOff}/${agg.finished} signed off (${agg.cells} cells)`}
              />
              <span className="font-mono text-[10px] text-faint">{band}</span>
              <span className="font-mono text-[9px] text-faint">
                {agg.signedOff}/{agg.finished}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function cellId(cell: HarnessCell): string {
  const k = cell.key;
  return [k.vendor, k.model, k.binary_version, k.capability, k.size_band].join(":");
}

function CellRow({ cell, floor }: { cell: HarnessCell; floor: number }) {
  const k = cell.key;
  const pct = cell.rate === null ? "—" : `${Math.round(cell.rate * 100)}%`;
  return (
    <div
      data-testid="harness-cell"
      className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3"
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-mono text-[12.5px]">
          {k.vendor}
          {k.model ? `:${k.model}` : ""}
        </span>
        <span className="font-mono text-[10.5px] text-faint">
          {cell.label ?? k.capability} · {k.size_band}
          {k.binary_version ? ` · ${k.binary_version}` : ""}
        </span>
        {cell.kind === "family" && (
          <Badge testid="harness-family-label" tone="faint">
            family rollup
          </Badge>
        )}
        {cell.kind === "other" && (
          <Badge testid="harness-other" tone="faint">
            other — {cell.finished}
          </Badge>
        )}
        <span
          className={`ml-auto text-[15px] font-semibold ${cell.below_floor ? "text-faint" : ""}`}
          data-testid="harness-rate"
        >
          {pct}
        </span>
        <span className="font-mono text-[10.5px] text-faint">
          {cell.signed_off}/{cell.finished} signed off
        </span>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        {cell.below_floor && (
          <Badge testid="harness-below-floor" tone="faint">
            below the floor — {cell.finished} of {floor} attempts
          </Badge>
        )}
        {cell.skew && (
          <Badge testid="harness-skew" tone="warn" icon={<Scale size={11} />}>
            sampled by preference — {Math.round(cell.skew.share * 100)}%{" "}
            {cell.skew.reason.replace("_", " ")}
          </Badge>
        )}
        {!cell.is_current_version && (
          <Badge testid="harness-old-version" tone="faint">
            not the current version
          </Badge>
        )}
        {cell.versions_seen.length > 1 && (
          <Badge testid="harness-versions-seen" tone="faint">
            versions: {cell.versions_seen.join(", ")}
          </Badge>
        )}
      </div>

      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 font-mono text-[10.5px] text-faint">
        <span data-testid="harness-sampling">{samplingLabel(cell.sampling)}</span>
        {cell.median_seconds !== null && <span>median {cell.median_seconds}s</span>}
        <span data-testid="harness-cost">
          {costLabel(cell.build_cost ?? cell.cost)}
        </span>
      </div>
      {cell.samples && (
        <div className="mt-1.5 font-mono text-[10.5px] text-muted" data-testid="harness-samples">
          natural {cell.samples.natural.signed_off}/{cell.samples.natural.n}
          {cell.samples.natural.rate === null ? "" : ` (${Math.round(cell.samples.natural.rate * 100)}%)`}
          {" · "}
          probe {cell.samples.probe.signed_off}/{cell.samples.probe.n}
          {cell.samples.probe.rate === null ? "" : ` (${Math.round(cell.samples.probe.rate * 100)}%)`}
          {cell.samples.probe.below_floor ? " — probe below the floor" : ""}
        </div>
      )}
      {cell.utilization && (
        <div className="mt-1.5 font-mono text-[10.5px] text-faint" data-testid="harness-utilization">
          {costLabel(cell.utilization.tokens)}
          {" · "}
          {cell.utilization.turns.reason
            ? cell.utilization.turns.reason
            : `median ${cell.utilization.turns.median} turns / budget ${cell.utilization.turns.budget_median} (n ${cell.utilization.turns.reported})`}
          {" · "}
          {cell.utilization.budget_hits.reason
            ? cell.utilization.budget_hits.reason
            : `budget hit ${Math.round((cell.utilization.budget_hits.share ?? 0) * 100)}% (n ${cell.utilization.budget_hits.reported})`}
        </div>
      )}
      {(cell.build_cost || cell.review_cost) && (
        <div className="mt-1 font-mono text-[10.5px] text-faint" data-testid="harness-build-review-cost">
          cost of build {costLabel(cell.build_cost ?? cell.cost)}
          {" · "}
          cost of review {cell.review_cost ? costLabel(cell.review_cost) : "not reported"}
        </div>
      )}

      {cell.platform && (
        <div className="mt-2 font-mono text-[10.5px]" data-testid="harness-platform">
          {cell.platform.rate === null ? (
            <span className="text-faint">{cell.platform.reason}</span>
          ) : (
            <span className="text-muted">
              platform average {Math.round(cell.platform.rate * 100)}% across{" "}
              {cell.platform.orgs} organisations (n {cell.platform.n})
            </span>
          )}
        </div>
      )}

      {cell.by_project && cell.by_project.length > 0 && (
        <div className="mt-1.5 font-mono text-[10.5px] text-faint" data-testid="harness-by-project">
          {cell.by_project
            .map((p) => `${p.project_id} ${p.signed_off}/${p.finished}`)
            .join(" · ")}
        </div>
      )}

      {cell.leaves && cell.leaves.length > 0 && (
        <div className="mt-2 flex flex-col gap-1" data-testid="harness-leaves">
          {cell.leaves.map((leaf) => (
            <div
              key={cellId(leaf)}
              data-testid="harness-leaf"
              data-below-floor={leaf.below_floor ? "true" : "false"}
              className={`flex items-baseline gap-2 font-mono text-[11px] ${
                leaf.below_floor ? "text-faint" : "text-muted"
              }`}
            >
              <span>{leaf.label ?? leaf.key.capability}</span>
              <span>
                {leaf.rate === null ? "—" : `${Math.round(leaf.rate * 100)}%`}
              </span>
              <span>
                {leaf.signed_off}/{leaf.finished}
                {leaf.below_floor ? " — below the floor" : ""}
              </span>
            </div>
          ))}
        </div>
      )}

      <Series points={cell.series} />
    </div>
  );
}

function samplingLabel(s: { first_choice: number; fallback: number; explicit: number; unknown: number; probe?: number }): string {
  const probe = s.probe ? ` · probe ${s.probe}` : "";
  return `first choice ${s.first_choice} · fallback ${s.fallback} · explicit ${s.explicit} · unknown ${s.unknown}${probe}`;
}

function costLabel(cost: HarnessCost): string {
  return cost.comparable
    ? `${cost.tokens_per_signed_off} tokens per signed-off item`
    : cost.reason;
}

function ReviewRow({ cell, floor }: { cell: HarnessReviewCell; floor: number }) {
  return (
    <div
      data-testid="harness-review-cell"
      className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3"
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-mono text-[12.5px]">
          {cell.key.vendor}
          {cell.key.model ? `:${cell.key.model}` : ""}
        </span>
        <span className="font-mono text-[10.5px] text-faint">
          {cell.key.capability} · {cell.key.size_band}
        </span>
        <Badge testid="harness-f2-label" tone="faint">
          {cell.f2.label}
        </Badge>
        <span className="ml-auto font-mono text-[10.5px] text-faint">
          {cell.checked} checked verdicts
        </span>
      </div>
      {cell.below_floor && (
        <div className="mt-2">
          <Badge testid="harness-review-below-floor" tone="faint">
            below the floor — {cell.checked} of {floor} checked verdicts
          </Badge>
        </div>
      )}
      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 font-mono text-[10.5px] text-muted">
        <span data-testid="harness-f1">
          F1 {cell.f1.rate === null ? "—" : `${Math.round(cell.f1.rate * 100)}%`} n={cell.f1.n}
        </span>
        <span data-testid="harness-f2">
          F2 miss {cell.f2.miss}/{cell.f2.n} unconfirmed {cell.f2.miss_unconfirmed}
        </span>
        <span data-testid="harness-f3">
          F3 unclassified {cell.f3.unclassified === null ? "—" : `${Math.round(cell.f3.unclassified * 100)}%`}
        </span>
      </div>
    </div>
  );
}

function Series({ points }: { points: HarnessPoint[] }) {
  if (points.length === 0) return null;
  return (
    <div className="mt-2.5 flex items-end gap-1" data-testid="harness-series">
      {points.map((p) => (
        <div
          key={p.week}
          data-testid="harness-point"
          data-week={p.week}
          data-below-floor={p.below_floor ? "true" : "false"}
          title={`${p.week}: ${p.signed_off}/${p.finished} signed off${
            p.below_floor ? " — below the floor" : ""
          }`}
          className="flex-1"
        >
          <div
            className={`w-full rounded-[2px] ${p.below_floor ? "bg-line-2" : "bg-st-done"}`}
            style={{ height: `${4 + Math.round((p.rate ?? 0) * 26)}px` }}
          />
          <div className="mt-1 truncate text-center font-mono text-[9px] text-faint">
            {p.week.slice(-3)}
          </div>
        </div>
      ))}
    </div>
  );
}

function Badge({
  children,
  testid,
  tone,
  icon,
}: {
  children: React.ReactNode;
  testid: string;
  tone: "faint" | "warn";
  icon?: React.ReactNode;
}) {
  const cls =
    tone === "warn"
      ? "border-[rgba(240,180,80,0.35)] bg-[rgba(240,180,80,0.1)] text-[#f0b450]"
      : "border-line-2 bg-surface text-faint";
  return (
    <span
      data-testid={testid}
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10.5px] ${cls}`}
    >
      {icon ?? (tone === "warn" ? <AlertTriangle size={11} /> : null)}
      {children}
    </span>
  );
}
