import { useFleet } from "@/lib/queries";
import type { FleetMeasured, HarnessGradingRule, HarnessReport } from "@/lib/types";

/**
 * PRD-47 S12 — what gbfleet is actually served, nothing invented.
 *
 * Every value on this tab comes from the server: the routing table from `measured`
 * (fleet_status), the grading rules from the harness report's `grading_rules` (the same
 * constants the backend rules read), and the fleet overview verbatim. Nothing is hardcoded.
 */
export function GuidanceTab({
  projectId,
  report,
}: {
  projectId: string;
  report: HarnessReport;
}) {
  const { data: fleet } = useFleet(projectId);
  const measured = fleet?.measured ?? [];

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      <GenerationStamp report={report} measuredCount={measured.length} />
      <RoutingTable measured={measured} floor={report.floor} />
      <GradingRules rules={report.grading_rules ?? []} floor={report.floor} />
      <AsServed fleet={fleet} />
    </div>
  );
}

function GenerationStamp({ report, measuredCount }: { report: HarnessReport; measuredCount: number }) {
  return (
    <div
      data-testid="guidance-generation-stamp"
      className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 font-mono text-[11.5px] text-muted"
    >
      <span>window {report.window_days} days</span>
      <span className="mx-2 text-faint">·</span>
      <span>floor {report.floor}</span>
      <span className="mx-2 text-faint">·</span>
      <span>{report.cells.length} cells</span>
      <span className="mx-2 text-faint">·</span>
      <span>{measuredCount} measured rows</span>
      <span className="mx-2 text-faint">·</span>
      <span>generated {new Date(report.generated_at).toLocaleString()}</span>
    </div>
  );
}

function RoutingTable({ measured, floor }: { measured: FleetMeasured[]; floor: number }) {
  if (measured.length === 0) {
    return (
      <div
        data-testid="guidance-routing-empty"
        className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3 text-[12.5px] text-muted"
      >
        No measured rows served. The routing table appears once a delegation finishes and
        reports its outcome.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2" data-testid="guidance-routing-table">
      <div className="text-[13px] font-semibold">Routing table — as measured</div>
      <div className="text-[11.5px] text-faint">
        Per vendor × model × capability from fleet_status.measured. Each band shows the
        signed-off rate and n. A band absent from the payload is shown as not measured.
      </div>
      <div className="overflow-x-auto rounded-[10px] border border-line-2">
        <table className="w-full text-left font-mono text-[11px]">
          <thead className="border-b border-line-2 bg-surface-2 text-faint">
            <tr>
              <th className="px-2.5 py-1.5">harness</th>
              <th className="px-2.5 py-1.5">capability</th>
              <th className="px-2.5 py-1.5">layer</th>
              <th className="px-2.5 py-1.5 text-right">quality</th>
              <th className="px-2.5 py-1.5 text-right">n</th>
              <th className="px-2.5 py-1.5 text-right">latency</th>
              <th className="px-2.5 py-1.5">bands (XS/S/M/L/XL)</th>
            </tr>
          </thead>
          <tbody>
            {measured.map((m, i) => (
              <MeasuredRow key={i} m={m} floor={floor} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function MeasuredRow({ m, floor }: { m: FleetMeasured; floor: number }) {
  const bandOrder = ["XS", "S", "M", "L", "XL"];
  const bands = m.bands ?? {};
  const bandCells = bandOrder.map((b) => {
    const entry = bands[b];
    if (!entry) return { band: b, label: "—" };
    const belowFloor = entry.n < floor;
    return {
      band: b,
      label: `${Math.round(entry.value * 100)}% (${entry.n})`,
      belowFloor,
    };
  });

  return (
    <tr data-testid="guidance-measured-row" className="border-b border-line-2 last:border-b-0">
      <td className="px-2.5 py-1.5">
        {m.vendor}{m.model ? `:${m.model}` : ""}
      </td>
      <td className="px-2.5 py-1.5">{m.capability ?? "—"}</td>
      <td className="px-2.5 py-1.5 text-faint">{m.layer ?? "—"}</td>
      <td className="px-2.5 py-1.5 text-right">
        {m.quality.n > 0 ? `${Math.round(m.quality.value * 100)}%` : "—"}
      </td>
      <td className="px-2.5 py-1.5 text-right text-faint">{m.quality.n}</td>
      <td className="px-2.5 py-1.5 text-right">
        {m.latency ? `${m.latency.median_seconds}s` : "—"}
      </td>
      <td className="px-2.5 py-1.5">
        <div className="flex gap-1.5">
          {bandCells.map((bc) => (
            <span
              key={bc.band}
              data-testid={`guidance-band-${bc.band}`}
              className={bc.belowFloor ? "text-faint" : ""}
              title={`${bc.band}: ${bc.label}`}
            >
              {bc.label}
            </span>
          ))}
        </div>
      </td>
    </tr>
  );
}

function GradingRules({ rules, floor }: { rules: HarnessGradingRule[]; floor: number }) {
  if (rules.length === 0) {
    return (
      <div
        data-testid="guidance-rules-empty"
        className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3 text-[12.5px] text-muted"
      >
        Grading rules not served by the backend.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2" data-testid="guidance-grading-rules">
      <div className="text-[13px] font-semibold">Grading rules — thresholds as served</div>
      <div className="text-[11.5px] text-faint">
        R1–R6 fire on cells above the {floor}-attempt floor. Thresholds come from the
        backend constants the rules read — not a copy on this page.
      </div>
      <div className="flex flex-col gap-1.5">
        {rules.map((r) => (
          <div
            key={r.rule}
            data-testid="guidance-rule"
            data-rule={r.rule}
            className="rounded-[8px] border border-line-2 bg-surface-2 px-3 py-2 font-mono text-[11.5px]"
          >
            <span className="rounded border border-line-2 px-1 py-0.5 text-[10px] text-faint">
              {r.rule}
            </span>
            <span className="ml-2 text-muted">{r.label}</span>
            <span className="ml-2 text-faint">
              {Object.entries(r.thresholds)
                .map(([k, v]) => `${k} ${v}`)
                .join(" · ")}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function AsServed({ fleet }: { fleet: ReturnType<typeof useFleet>["data"] }) {
  if (!fleet) {
    return (
      <div
        data-testid="guidance-as-served-empty"
        className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3 text-[12.5px] text-muted"
      >
        Fleet status not yet loaded.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2" data-testid="guidance-as-served">
      <div className="text-[13px] font-semibold">As served — fleet_status verbatim</div>
      <div className="text-[11.5px] text-faint">
        The raw fleet_status payload the supervisor received. Nothing curated.
      </div>
      <pre
        data-testid="guidance-as-served-json"
        className="max-h-96 overflow-auto rounded-[10px] border border-line-2 bg-surface-2 p-3 font-mono text-[10.5px] text-muted"
      >
        {JSON.stringify(fleet, null, 2)}
      </pre>
    </div>
  );
}
