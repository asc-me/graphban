import type { FleetMatrixRow, FleetOverview } from "@/lib/types";

const GRADING_RULES = [
  {
    id: "R1",
    title: "Promote",
    detail: "An unverified row this good, this often, is worth a verified commit.",
    thresholds: "min_finished: 10 · min_rate: 80%",
  },
  {
    id: "R2",
    title: "Demote",
    detail: "A verified row this bad is worth a failed entry.",
    thresholds: "min_finished: 6 · max_rate: 25%",
  },
  {
    id: "R3",
    title: "Reweight",
    detail: "A profile's top default beaten by this margin across this many families.",
    thresholds: "min_n: 8 · margin: 30% · min_families: 2",
  },
  {
    id: "R4",
    title: "Policy",
    detail: "A local_only project whose local rows bounce or succeed this often.",
    thresholds: "min_n: 6 · bounce_rate: 70% · keep_rate: 70%",
  },
  {
    id: "R5",
    title: "Reprioritise",
    detail: "A cell at the floor that disagrees with the row's per-capability prior.",
    thresholds: "min_n: 5 · margin: 30%",
  },
  {
    id: "R6",
    title: "Install",
    detail: "Resolutions dropped a better measured row as not installed this often.",
    thresholds: "min_drops: 6 · margin: 30%",
  },
];

const SIZE_BAND_ORDER = ["XS", "S", "M", "L", "XL"];

function statusTone(status: string): string {
  switch (status) {
    case "verified":
      return "text-st-done";
    case "failed":
      return "text-red-400";
    case "unverified":
      return "text-faint";
    case "unregistered":
      return "text-faint";
    default:
      return "text-muted";
  }
}

/**
 * PRD-47 S12: what the supervisor actually served — the routing table, the grading rules,
 * the generation stamp, and the verbatim fleet_status text. Nothing here chooses; it
 * records what was handed to gbfleet and the rules that judge it.
 */
export function GuidanceTab({
  matrix,
  generatedAt,
  fleetStatus,
}: {
  matrix: FleetMatrixRow[] | undefined;
  generatedAt: string | undefined;
  fleetStatus: FleetOverview | undefined | null;
}) {
  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      <RoutingTable rows={matrix} />
      <GradingRules />
      <GenerationStamp generatedAt={generatedAt} />
      <AsServed fleetStatus={fleetStatus} />
    </div>
  );
}

function RoutingTable({ rows }: { rows: FleetMatrixRow[] | undefined }) {
  if (!rows || rows.length === 0) {
    return (
      <div
        data-testid="guidance-no-matrix"
        className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-[12.5px] text-muted"
      >
        No routing table served. The matrix has not been committed.
      </div>
    );
  }
  const grouped = new Map<string, FleetMatrixRow[]>();
  for (const row of rows) {
    const key = `${row.harness}:${row.model}`;
    const group = grouped.get(key) ?? [];
    group.push(row);
    grouped.set(key, group);
  }
  return (
    <div className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
      <div className="mb-2 text-[13px] font-semibold">Routing table</div>
      <div className="overflow-x-auto">
        <table className="w-full text-left font-mono text-[11px]">
          <thead>
            <tr className="border-b border-line-2 text-faint">
              <th className="pb-1 pr-3">harness</th>
              <th className="pb-1 pr-3">lane</th>
              <th className="pb-1 pr-3">tier</th>
              <th className="pb-1 pr-3">status</th>
              <th className="pb-1 pr-3">cost</th>
              <th className="pb-1">local</th>
            </tr>
          </thead>
          <tbody data-testid="guidance-routing-table">
            {rows.map((row, i) => (
              <tr key={i} className="border-b border-line-2/50">
                <td className="py-1 pr-3 text-muted">
                  {row.harness}:{row.model}
                </td>
                <td className="py-1 pr-3 text-muted">{row.lane}</td>
                <td className="py-1 pr-3">{row.tier}</td>
                <td className={`py-1 pr-3 ${statusTone(row.status)}`}>{row.status}</td>
                <td className="py-1 pr-3">{row.cost_class}</td>
                <td className="py-1">{row.local ? "yes" : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-1.5 font-mono text-[10.5px] text-faint">
        {rows.length} rows across {grouped.size} harness:model combinations
      </div>
    </div>
  );
}

function GradingRules() {
  return (
    <div className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
      <div className="mb-2 text-[13px] font-semibold">Grading rules</div>
      <div className="flex flex-col gap-2" data-testid="guidance-grading-rules">
        {GRADING_RULES.map((rule) => (
          <div key={rule.id} data-testid="guidance-rule" data-rule={rule.id}>
            <div className="flex items-baseline gap-2">
              <span className="rounded border border-line-2 px-1.5 py-0.5 font-mono text-[10px] text-faint">
                {rule.id}
              </span>
              <span className="text-[12.5px] font-medium">{rule.title}</span>
            </div>
            <p className="mt-0.5 text-[12px] text-muted">{rule.detail}</p>
            <div className="mt-0.5 font-mono text-[10.5px] text-faint">{rule.thresholds}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function GenerationStamp({ generatedAt }: { generatedAt: string | undefined }) {
  if (!generatedAt) return null;
  return (
    <div
      data-testid="guidance-generation-stamp"
      className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 font-mono text-[11px] text-faint"
    >
      Generated at {new Date(generatedAt).toLocaleString()}
    </div>
  );
}

function AsServed({ fleetStatus }: { fleetStatus: FleetOverview | undefined | null }) {
  if (!fleetStatus) {
    return (
      <div
        data-testid="guidance-no-fleet-status"
        className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-[12.5px] text-muted"
      >
        No fleet status available. The supervisor has not reported.
      </div>
    );
  }
  const served: Record<string, unknown> = {
    online: fleetStatus.online,
    total: fleetStatus.total,
    by_role: fleetStatus.by_role,
    posture: fleetStatus.posture,
    measured: fleetStatus.measured,
    heartbeat_interval_seconds: fleetStatus.heartbeat_interval_seconds,
    presence_ttl_seconds: fleetStatus.presence_ttl_seconds,
  };
  return (
    <div className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
      <div className="mb-2 text-[13px] font-semibold">As served</div>
      <pre
        data-testid="guidance-as-served"
        className="overflow-x-auto rounded-[6px] border border-line-2 bg-surface px-3 py-2 font-mono text-[11px] text-muted"
      >
        {JSON.stringify(served, null, 2)}
      </pre>
    </div>
  );
}

export { SIZE_BAND_ORDER };
