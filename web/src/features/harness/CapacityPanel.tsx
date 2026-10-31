import { useHarnessCapacity } from "@/lib/queries";

/**
 * PRD-62 S5 — per entitlement: headroom state, reset cadence, this week's tokens and a
 * projected exhaustion time; then the org rollup. Every absence says why: a projection with no
 * date gives its reason, an unreported spend is a count (and makes the rollup share a floor), and a rollup number the supervisor does
 * not record yet reads "not recorded" rather than 0. Hidden when the org is not on a plan that
 * has capacity routing (the endpoint answers 402).
 */
export function CapacityPanel({ projectId }: { projectId?: string }) {
  const { data } = useHarnessCapacity(projectId);
  if (!data) return null;
  const u = data.utilization;
  const count = (c: { count: number | null }) => (c.count === null ? "not recorded" : c.count);
  return (
    <section data-testid="harness-capacity" className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
      <div className="text-[13px] font-medium">Capacity</div>
      <div data-testid="capacity-rollup" className="mt-1 text-body text-muted">
        Headroom used last week:{" "}
        {u.share_used === null
          ? "unknown — no entitlement states a token budget"
          : u.share_is_lower_bound
            ? `≥${Math.round(u.share_used * 100)}% of ${u.budget} tokens (+${u.unreported_spends} unreported)`
            : `${Math.round(u.share_used * 100)}% of ${u.budget} tokens`}
        . Waves ended by capacity: {count(u.waves_ended_by_capacity)}. Items deferred to a reset:{" "}
        {count(u.items_deferred_to_reset)}.
      </div>
      {data.entitlements.length === 0 ? (
        <p className="mt-2 text-body text-muted">No entitlements registered.</p>
      ) : (
        <table className="mt-2 w-full text-left text-body">
          <thead className="text-small text-faint">
            <tr>
              <th>Entitlement</th><th>Headroom</th><th>Resets</th><th>Tokens this week</th><th>Projected exhaustion</th>
            </tr>
          </thead>
          <tbody>
            {data.entitlements.map((e) => (
              <tr key={e.id} data-testid="capacity-row" data-state={e.state}>
                <td>{e.vendor} {e.plan_label || e.kind}</td>
                <td title={e.vendor_text}>{e.state}</td>
                <td>{e.reset_cadence ?? "not stated"}</td>
                <td>
                  {e.week.tokens}
                  {e.week.unreported > 0 && ` (+${e.week.unreported} unreported)`}
                </td>
                <td title={e.projected_exhaustion.basis}>
                  {e.projected_exhaustion.at
                    ? new Date(e.projected_exhaustion.at).toLocaleString()
                    : `— ${e.projected_exhaustion.reason}`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
