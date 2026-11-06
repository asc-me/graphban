import type { HarnessRoutingDecisions } from "@/lib/types";

/** What the router did with the headroom above: one group per verdict, reasons verbatim. */
export function RoutingDecisions({ decisions }: { decisions: HarnessRoutingDecisions }) {
  return (
    <div data-testid="capacity-routing">
      <div className="text-[13px] font-medium">Routing decisions</div>
      <p className="text-body text-muted">What the router did with the headroom above.</p>
      {decisions.reason && (
        <p data-testid="capacity-routing-reason" className="mt-1 text-body text-muted">{decisions.reason}</p>
      )}
      <div className="mt-2 flex flex-col gap-2">
        {decisions.groups.map((g) => (
          <div key={g.verdict} data-testid="capacity-routing-group" data-verdict={g.verdict}>
            <div className="font-mono text-small">
              {g.label} · <span data-testid="capacity-routing-count">{g.count === null ? "not recorded" : g.count}</span>
            </div>
            {g.count === null && g.note && <div className="text-small text-muted">{g.note}</div>}
            {g.recent.map((r, i) => (
              <div key={i} data-testid="capacity-routing-row" className="text-small text-muted">
                <span className="font-mono">{r.item_key}</span> · {r.entitlement_id ?? "no entitlement"} · “{r.reason}” ·{" "}
                {new Date(r.at).toLocaleString()}
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
