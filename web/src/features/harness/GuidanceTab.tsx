import type { FleetMatrixRow, FleetOverview, HarnessReport } from "@/lib/types";

const GRADING_RULES = [
  { id: "R1", title: "Promote a cell", detail: "A cell above the floor signed off at or above the threshold and its matrix row is still unverified." },
  { id: "R2", title: "Demote a cell", detail: "A cell above the floor signed off below the threshold and its matrix row is still verified." },
  { id: "R3", title: "Adjust profile weight", detail: "A measured axis diverges from the profile weight by more than the threshold." },
  { id: "R4", title: "Adjust policy constraint", detail: "A policy cap is consistently hit or never hit across the window." },
  { id: "R5", title: "Flag a skew", detail: "A cell above the floor is sampled overwhelmingly by one path." },
  { id: "R6", title: "Flag a thin cell", detail: "A cell is above the floor but its replay considered too few resolutions to speak for." },
];

const RULE_THRESHOLDS: Record<string, string> = {
  R1: "min_finished: 10, min_rate: 0.8",
  R2: "min_finished: 10, max_rate: 0.5",
  R3: "weight_delta: 0.2, min_n: 5",
  R4: "hit_share: 0.8 | 0.05, min_n: 10",
  R5: "skew_share: 0.8, min_finished: 5",
  R6: "min_finished: 5, max_replay: 3",
};

function statusLabel(row: FleetMatrixRow): string {
  return row.status === "verified" ? "verified" : row.status;
}

/**
 * PRD-47 S12 — what the supervisor was actually served: the routing matrix, the grading
 * rules that produce recommendations, and the raw fleet_status text. Nothing here is
 * interpreted — it is the input, not the output.
 */
export function GuidanceTab({
  fleetData,
  harnessReport,
}: {
  fleetData?: FleetOverview;
  harnessReport?: HarnessReport;
}) {
  const matrixRows = fleetData?.matrix?.rows ?? [];
  const generatedAt = harnessReport?.generated_at;

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      <section data-testid="guidance-routing-table">
        <h3 className="mb-2 text-[13px] font-semibold">Routing table</h3>
        <p className="mb-3 text-[12px] text-muted">
          The matrix the supervisor resolves against. Each row is a harness, model, lane and
          tier the supervisor may pick. A row that is not verified is still considered — the
          supervisor scores on measured axes and says so.
        </p>
        {matrixRows.length === 0 ? (
          <div className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-[12.5px] text-muted">
            No routing matrix served. The supervisor resolves on the harness catalog alone.
          </div>
        ) : (
          <div className="overflow-x-auto rounded-[10px] border border-line-2">
            <table className="w-full text-left font-mono text-[11.5px]">
              <thead>
                <tr className="border-b border-line-2 bg-surface-2">
                  <th className="px-3 py-2 text-faint">harness</th>
                  <th className="px-3 py-2 text-faint">model</th>
                  <th className="px-3 py-2 text-faint">vendor</th>
                  <th className="px-3 py-2 text-faint">lane</th>
                  <th className="px-3 py-2 text-faint">tier</th>
                  <th className="px-3 py-2 text-faint">verdict</th>
                  <th className="px-3 py-2 text-faint">cost</th>
                  <th className="px-3 py-2 text-faint">local</th>
                </tr>
              </thead>
              <tbody>
                {matrixRows.map((row, i) => (
                  <tr
                    key={`${row.harness}:${row.model}:${row.lane}:${row.tier}`}
                    data-testid="guidance-routing-row"
                    className={i % 2 === 0 ? "bg-surface" : "bg-surface-2"}
                  >
                    <td className="px-3 py-1.5 text-muted">{row.harness}</td>
                    <td className="px-3 py-1.5 text-muted">{row.model}</td>
                    <td className="px-3 py-1.5 text-faint">{row.vendor}</td>
                    <td className="px-3 py-1.5 text-faint">{row.lane}</td>
                    <td className="px-3 py-1.5 text-faint">{row.tier}</td>
                    <td className="px-3 py-1.5">
                      <span
                        data-testid="guidance-routing-verdict"
                        className={row.status === "verified" ? "text-st-done" : "text-faint"}
                      >
                        {statusLabel(row)}
                      </span>
                    </td>
                    <td className="px-3 py-1.5 text-faint">{row.cost_class}</td>
                    <td className="px-3 py-1.5 text-faint">{row.local ? "yes" : "no"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section data-testid="guidance-grading-rules">
        <h3 className="mb-2 text-[13px] font-semibold">Grading rules</h3>
        <p className="mb-3 text-[12px] text-muted">
          R1–R6 produce the recommendation cards on the Changes tab. Each fires on cells
          above the floor; nothing fires on a cell that has not earned its count.
        </p>
        <div className="flex flex-col gap-2">
          {GRADING_RULES.map((rule) => (
            <div
              key={rule.id}
              data-testid="guidance-rule"
              data-rule={rule.id}
              className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5"
            >
              <div className="flex items-baseline gap-2">
                <span className="rounded-full border border-line-2 px-1.5 py-0.5 font-mono text-[10px] text-faint">
                  {rule.id}
                </span>
                <span className="text-[12.5px] font-medium">{rule.title}</span>
              </div>
              <p className="mt-1 text-[12px] text-muted">{rule.detail}</p>
              <div className="mt-1 font-mono text-[10.5px] text-faint">
                {RULE_THRESHOLDS[rule.id]}
              </div>
            </div>
          ))}
        </div>
      </section>

      {generatedAt && (
        <section data-testid="guidance-generation-stamp">
          <h3 className="mb-1 text-[13px] font-semibold">Generation stamp</h3>
          <p className="font-mono text-[11.5px] text-faint">
            Report generated at {new Date(generatedAt).toLocaleString()}
          </p>
        </section>
      )}

      <section data-testid="guidance-as-served">
        <h3 className="mb-2 text-[13px] font-semibold">As served</h3>
        <p className="mb-3 text-[12px] text-muted">
          The verbatim <span className="font-mono">fleet_status</span> text the supervisor
          received. This is the raw input — nothing on this page interprets it.
        </p>
        <pre
          data-testid="guidance-fleet-status"
          className="max-h-80 overflow-auto rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 font-mono text-[11px] text-muted"
        >
          {fleetData ? JSON.stringify(fleetStatusSummary(fleetData), null, 2) : "no fleet_status served"}
        </pre>
      </section>
    </div>
  );
}

function fleetStatusSummary(fleet: FleetOverview): Record<string, unknown> {
  return {
    posture: fleet.posture,
    online: fleet.online,
    total: fleet.total,
    by_role: fleet.by_role,
    roles: fleet.roles,
    measured: fleet.measured,
    matrix: fleet.matrix ?? null,
    mix: fleet.mix ?? null,
  };
}
