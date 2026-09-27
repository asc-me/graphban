import { useState } from "react";

import type { FleetMatrixRow, FleetOverview, HarnessReport } from "@/lib/types";

/**
 * PRD-47 S12 — what the supervisor was actually told when it asked for a routing
 * decision. The routing table is the matrix served to gbfleet; the grading rules are
 * the thresholds R1–R6 that fire recommendations; the generation stamp says when the
 * report was produced; and "As served" is the verbatim fleet_status text.
 */
export function GuidanceTab({
  data,
  fleetData,
}: {
  data: HarnessReport;
  fleetData: Partial<FleetOverview> | undefined;
}) {
  const [showRaw, setShowRaw] = useState(false);
  const matrix = fleetData?.matrix?.rows ?? [];

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-3">
      <div className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-[12.5px] text-muted">
        What the supervisor was told. The routing table is the matrix served to gbfleet
        per function and effort band; the grading rules are the thresholds that fire
        recommendations on the Changes tab. Nothing here is a preference — it is what
        was actually on the wire.
      </div>

      <GenerationStamp data={data} />
      <RoutingTable rows={matrix} />
      <GradingRules />
      <AsServed
        fleetData={fleetData}
        showRaw={showRaw}
        onToggle={() => setShowRaw((v) => !v)}
      />
    </div>
  );
}

function GenerationStamp({ data }: { data: HarnessReport }) {
  return (
    <div
      data-testid="harness-generation-stamp"
      className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 font-mono text-[11px] text-faint"
    >
      <span className="text-muted">Generation stamp</span>
      {" — "}
      generated at {data.generated_at}
      {data.snapshot_at ? ` · snapshot ${data.snapshot_at}` : ""}
      {" · "}
      window {data.window_days} days · floor {data.floor} · {data.cells.length} cells
    </div>
  );
}

function RoutingTable({ rows }: { rows: FleetMatrixRow[] }) {
  if (rows.length === 0) {
    return (
      <div
        data-testid="harness-routing-empty"
        className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3 text-[12.5px] text-muted"
      >
        No routing table served. The matrix is empty — no harness has been verified.
      </div>
    );
  }

  return (
    <div data-testid="harness-routing-table" className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
      <div className="mb-2 text-[13px] font-semibold">Routing table</div>
      <div className="mb-1 font-mono text-[10.5px] text-faint">
        The matrix served to gbfleet — harness, model, vendor, lane, tier, status
      </div>
      <div className="overflow-x-auto">
        <table className="w-full font-mono text-[11px]">
          <thead>
            <tr className="border-b border-line-2 text-faint">
              <th className="pb-1 pr-3 text-left font-medium">Harness</th>
              <th className="pb-1 pr-3 text-left font-medium">Model</th>
              <th className="pb-1 pr-3 text-left font-medium">Vendor</th>
              <th className="pb-1 pr-3 text-left font-medium">Lane</th>
              <th className="pb-1 pr-3 text-left font-medium">Tier</th>
              <th className="pb-1 pr-3 text-left font-medium">Status</th>
              <th className="pb-1 text-left font-medium">Cost</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr
                key={`${row.harness}:${row.model}:${row.vendor}:${row.lane}:${row.tier}`}
                data-testid="harness-routing-row"
                className={i % 2 === 0 ? "" : "bg-surface"}
              >
                <td className="py-1 pr-3 text-muted">{row.harness}</td>
                <td className="py-1 pr-3 text-muted">{row.model || "—"}</td>
                <td className="py-1 pr-3 text-muted">{row.vendor}</td>
                <td className="py-1 pr-3 text-muted">{row.lane}</td>
                <td className="py-1 pr-3 text-muted">{row.tier}</td>
                <td className="py-1 pr-3">
                  <StatusBadge status={row.status} />
                </td>
                <td className="py-1 text-muted">{row.cost_class}{row.local ? " · local" : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    verified: "text-st-done",
    unverified: "text-faint",
    failed: "text-[#f06464]",
    unregistered: "text-[#f0b450]",
  };
  return (
    <span className={colors[status] ?? "text-muted"}>
      {status}
    </span>
  );
}

const GRADING_RULES = [
  {
    rule: "R1",
    title: "Promote to verified",
    detail: "An unverified row this good, this often, is worth a verified commit.",
    thresholds: "min 10 finished · min 80% signed-off",
  },
  {
    rule: "R2",
    title: "Demote to failed",
    detail: "A verified row this bad is worth a failed entry.",
    thresholds: "min 6 finished · max 25% signed-off",
  },
  {
    rule: "R3",
    title: "Reweight defaults",
    detail: "A profile's top default beaten by this much, on this many attempts, in the same cell.",
    thresholds: "min 8 attempts · min 30% margin · across 2+ families",
  },
  {
    rule: "R4",
    title: "Policy nudge",
    detail: "A local_only project whose local rows bounce this often, or succeed this often.",
    thresholds: "min 6 attempts · 70% bounce or keep rate",
  },
  {
    rule: "R5",
    title: "Prior disagreement",
    detail: "A cell at the floor that disagrees with the row's per-capability prior.",
    thresholds: "min 5 attempts · min 30% margin",
  },
  {
    rule: "R6",
    title: "Better row dropped",
    detail: "Resolutions dropped a better measured row as not installed.",
    thresholds: "min 6 drops · min 30% margin",
  },
];

function GradingRules() {
  return (
    <div data-testid="harness-grading-rules" className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
      <div className="mb-2 text-[13px] font-semibold">Grading rules</div>
      <div className="mb-2 font-mono text-[10.5px] text-faint">
        The thresholds that fire recommendations on the Changes tab. R1–R6 read cells
        above the floor and produce drafts — none of them change anything.
      </div>
      <div className="flex flex-col gap-2">
        {GRADING_RULES.map((r) => (
          <div
            key={r.rule}
            data-testid="harness-grading-rule"
            data-rule={r.rule}
            className="rounded-[8px] border border-line-2 bg-surface px-3 py-2"
          >
            <div className="flex items-baseline gap-2">
              <span className="rounded-full border border-line-2 px-1.5 py-0.5 font-mono text-[10px] text-faint">
                {r.rule}
              </span>
              <span className="text-[12.5px] font-medium">{r.title}</span>
            </div>
            <p className="mt-1 text-[12px] text-muted">{r.detail}</p>
            <div className="mt-1 font-mono text-[10.5px] text-faint">{r.thresholds}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function AsServed({
  fleetData,
  showRaw,
  onToggle,
}: {
  fleetData: Partial<FleetOverview> | undefined;
  showRaw: boolean;
  onToggle: () => void;
}) {
  const raw = fleetData ? JSON.stringify(fleetData, null, 2) : "";

  return (
    <div data-testid="harness-as-served" className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
      <div className="flex items-center justify-between">
        <div className="text-[13px] font-semibold">As served</div>
        <button
          type="button"
          data-testid="harness-as-served-toggle"
          onClick={onToggle}
          className="font-mono text-[10.5px] text-faint hover:text-muted"
        >
          {showRaw ? "hide raw" : "show raw fleet_status"}
        </button>
      </div>
      <div className="mt-1 font-mono text-[10.5px] text-faint">
        The verbatim fleet_status text — what the supervisor actually received.
      </div>
      {showRaw && (
        <pre
          data-testid="harness-as-served-raw"
          className="mt-2 max-h-80 overflow-auto rounded-[6px] border border-line-2 bg-surface p-2.5 font-mono text-[10.5px] text-muted"
        >
          {raw || "{}"}
        </pre>
      )}
      {!showRaw && fleetData && (
        <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 font-mono text-[11px]">
          <div className="text-faint">online</div>
          <div className="text-muted">{fleetData.online ?? "—"}</div>
          <div className="text-faint">total agents</div>
          <div className="text-muted">{fleetData.total ?? "—"}</div>
          <div className="text-faint">posture</div>
          <div className="text-muted">{fleetData.posture ?? "—"}</div>
          <div className="text-faint">heartbeat interval</div>
          <div className="text-muted">{fleetData.heartbeat_interval_seconds ?? "—"}s</div>
          <div className="text-faint">presence TTL</div>
          <div className="text-muted">{fleetData.presence_ttl_seconds ?? "—"}s</div>
          {fleetData.measured && (
            <>
              <div className="text-faint">measured cells</div>
              <div className="text-muted">{fleetData.measured.length}</div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
