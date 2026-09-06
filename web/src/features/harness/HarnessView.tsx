import { useState } from "react";
import { AlertTriangle, Scale } from "lucide-react";

import { useProjectCtx } from "@/features/ProjectContext";
import { useHarness } from "@/lib/queries";
import type { HarnessCell, HarnessPoint, HarnessSampling } from "@/lib/types";

/**
 * PRD-38 PR 2 — how each harness has actually turned out, week by week.
 *
 * The page's whole job is to make a number and its warrant inseparable. Every rate carries
 * its `n`; a rate under the sample floor is drawn grey and says so; a cell whose samples came
 * overwhelmingly from one reason carries a badge saying it is not a fair comparison; and the
 * cost proxy either compares or states in words why it will not. Nothing here recommends
 * anything — that is PR 3 — and nothing here changes a preference.
 */
export function HarnessView() {
  const { activeId } = useProjectCtx();
  const [versions, setVersions] = useState<"current" | "all">("current");
  const { data, isLoading } = useHarness(activeId, { versions });

  if (isLoading || !data) {
    return (
      <div className="flex h-full items-center justify-center text-[13px] text-muted">Loading…</div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="harness-view">
      <div className="flex flex-none items-center gap-4 border-b border-line px-5 py-4">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight">Harness</h1>
          <p className="mt-0.5 text-[12.5px] text-muted">
            How each model and harness has turned out, per lane, task class and size band, over
            the last {data.window_days} days. A rate under {data.floor} finished attempts is
            shown grey because it is not yet a measurement.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <label className="font-mono text-[10.5px] text-faint" htmlFor="harness-versions">
            VERSIONS
          </label>
          <select
            id="harness-versions"
            aria-label="Binary versions"
            data-testid="harness-versions"
            className="rounded-[8px] border border-line-2 bg-surface-2 px-2 py-1 text-[12px]"
            value={versions}
            onChange={(e) => setVersions(e.target.value as "current" | "all")}
          >
            <option value="current">Current only</option>
            <option value="all">Every version</option>
          </select>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {data.cells.length === 0 ? (
          <div className="mx-auto mt-16 max-w-md text-center text-[13px] text-muted">
            Nothing measured yet. A cell appears here once a delegation finishes — one row per
            vendor, model, lane, tier, task class and size band.
          </div>
        ) : (
          <div className="mx-auto flex max-w-4xl flex-col gap-3">
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
            {data.cells.map((cell) => (
              <CellRow key={cellId(cell)} cell={cell} floor={data.floor} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function cellId(cell: HarnessCell): string {
  const k = cell.key;
  return [k.vendor, k.model, k.binary_version, k.lane, k.tier, k.task_class, k.size_band].join(":");
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
          {k.lane} · {k.tier} · {k.task_class} · {k.size_band}
          {k.binary_version ? ` · ${k.binary_version}` : ""}
        </span>
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
          {cell.cost.comparable
            ? `${cell.cost.tokens_per_signed_off} tokens per signed-off item`
            : cell.cost.reason}
        </span>
      </div>

      <Series points={cell.series} />
    </div>
  );
}

function samplingLabel(s: HarnessSampling): string {
  return `first choice ${s.first_choice} · fallback ${s.fallback} · explicit ${s.explicit} · unknown ${s.unknown}`;
}

/**
 * The trend, drawn as one bar per recorded week.
 *
 * Weeks with no attempts are simply absent from the data and stay absent here: a gap is what
 * "nobody ran anything" looks like, and joining across it would draw a line through a week
 * nobody measured. Thin weeks are grey rather than dropped, for the same reason the totals
 * show them.
 */
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
