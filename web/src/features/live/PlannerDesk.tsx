import { Link } from "react-router-dom";

import { cn } from "@/lib/cn";
import type { FleetOverview, LiveAgent } from "@/lib/types";

import { ROLE_TONE, heldBackClusters, plannerVerb } from "./liveUtils";

export function PlannerDesk({
  planners,
  fleet,
  trackerTo,
}: {
  planners: LiveAgent[];
  fleet: FleetOverview | undefined;
  trackerTo: string;
}) {
  const heldBack = heldBackClusters(fleet);

  return (
    <section className="rounded-[11px] border border-line-2 bg-surface-2 p-3.5">
      <h2 className="text-[13px] font-semibold">Planner desk</h2>
      <p className="mt-0.5 text-[11.5px] text-muted">
        Allocations, recent decisions, and work held back by area collisions.
      </p>

      {planners.length === 0 ? (
        <div className="mt-3 text-[12px] text-muted">No planner registered on this project.</div>
      ) : (
        <div className="mt-3 space-y-3">
          {planners.map((p) => (
            <PlannerBlock key={p.id} planner={p} trackerTo={trackerTo} />
          ))}
        </div>
      )}

      <div className="mt-4 border-t border-line-2 pt-3">
        <h3 className="font-mono text-[10px] uppercase tracking-wide text-faint">Held back</h3>
        {heldBack.length === 0 ? (
          <div className="mt-1.5 text-[12px] text-muted">Nothing waiting on a colliding area.</div>
        ) : (
          <ul className="mt-1.5 space-y-1.5">
            {heldBack.map((c, i) => (
              <li key={i} className="rounded-[9px] border border-dashed border-line-2 px-2.5 py-1.5 text-[11.5px]">
                <span className="font-mono text-fg-2">{c.areas.join(", ") || "—"}</span>
                <span className="ml-2 text-muted">held by {c.held_by}</span>
                {c.blocked_on && (
                  <span className="ml-1 text-[color:var(--color-st-blocked)]">· collides on {c.blocked_on}</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function PlannerBlock({ planner: p, trackerTo }: { planner: LiveAgent; trackerTo: string }) {
  const d = p.delegations;
  const verb = p.last_call ? plannerVerb(p.last_call.tool) : null;
  return (
    <div className="rounded-[9px] border border-line-2 bg-surface-1 px-2.5 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className={cn("rounded-md border px-1.5 py-0.5 font-mono text-[10px]", ROLE_TONE.planner)}>
          planner
        </span>
        <span className="text-[12.5px] text-fg-2">{p.label || p.id}</span>
        {verb && p.last_call && (
          <span className="rounded border border-line-2 px-1.5 py-0.5 font-mono text-[10px] text-fg-2">
            {verb} · {p.last_call.tool}
            {p.last_call.target ? ` · ${p.last_call.target}` : ""}
          </span>
        )}
      </div>
      {!d || d.rows.length === 0 ? (
        <div className="mt-1.5 text-[11.5px] text-faint">no open allocations</div>
      ) : (
        <ul className="mt-1.5 space-y-0.5 font-mono text-[10.5px] text-muted">
          {d.rows.slice(0, 6).map((r) => (
            <li key={r.id}>
              <Link to={trackerTo} className="text-fg-2 underline decoration-dotted">
                {r.item}
              </Link>
              <span className="ml-1.5">{r.state}</span>
              {r.agent_id && <span className="ml-1.5">→ {r.agent_id}</span>}
              <span className="ml-1.5">{r.lane}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
