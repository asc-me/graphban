import type { HarnessCapacity, HarnessRoutingDecisions } from "@/lib/types";
import { shortWhen } from "./capacityLook";

const GLYPH: Record<string, { g: string; c: string; k: string }> = {
  claimed: { g: "●", c: "text-st-done", k: "CLAIMED" },
  held: { g: "▣", c: "text-st-done", k: "RESERVATION" },
  deferred: { g: "◷", c: "text-muted-2", k: "DEFERRED" },
  review_reserve: { g: "◐", c: "text-[#f0b450]", k: "REVIEW RESERVE" },
  next_rung: { g: "↓", c: "text-muted-2", k: "NEXT RUNG" },
};

function Group({ k, count, children, testid, verdict }: {
  k: string; count: string; children: React.ReactNode; testid: string; verdict: string;
}) {
  return (
    <div data-testid={testid} data-verdict={verdict} className="grid grid-cols-[96px_minmax(0,1fr)] gap-2.5 border-t border-line-2/70 px-4 py-2.5">
      <span className="flex flex-col gap-0.5 pt-0.5">
        <span className="font-mono text-meta tracking-[0.6px] text-faint">{k}</span>
        <span data-testid="capacity-routing-count" className="font-mono text-meta text-faint/80">{count}</span>
      </span>
      <span className="flex min-w-0 flex-col gap-1.5">{children}</span>
    </div>
  );
}

function Line({ g, c, t, s, testid }: { g: string; c: string; t: string; s: string; testid?: string }) {
  return (
    <span data-testid={testid} className="flex flex-col gap-px">
      <span className="flex items-baseline gap-1.5 font-mono text-[11.5px] leading-[1.45] text-fg">
        <span aria-hidden="true" className={c}>{g}</span>
        <span className="min-w-0 break-words">{t}</span>
      </span>
      {s && <span className="pl-4 text-small text-muted">{s}</span>}
    </span>
  );
}

/**
 * What the router did with the headroom: one group per verdict the server serves, reasons
 * verbatim. A verdict the router does not record says so instead of showing 0. Waves ended by
 * capacity come from the supervisor's own count, which may also be unrecorded.
 */
export function RoutingDecisions({
  decisions,
  wavesEnded,
}: {
  decisions: HarnessRoutingDecisions;
  wavesEnded?: HarnessCapacity["utilization"]["waves_ended_by_capacity"];
}) {
  return (
    <div data-testid="capacity-routing" className="flex flex-col rounded-[12px] border border-line-2 bg-surface-2">
      <div className="flex flex-wrap items-baseline gap-2.5 px-4 py-3">
        <h3 className="text-[13.5px] font-semibold">Routing decisions</h3>
        <span className="text-[12px] text-muted">What the router did with the headroom · last 7 days.</span>
      </div>
      {decisions.reason && (
        <p data-testid="capacity-routing-reason" className="border-t border-line-2/70 px-4 py-2.5 text-[12px] text-muted">{decisions.reason}</p>
      )}
      {decisions.groups.map((grp) => {
        const look = GLYPH[grp.verdict] ?? { g: "·", c: "text-muted", k: grp.label.toUpperCase() };
        return (
          <Group key={grp.verdict} testid="capacity-routing-group" verdict={grp.verdict} k={look.k}
                 count={grp.count === null ? "not recorded" : `${grp.count} · 7d`}>
            {grp.count === null ? (
              <Line g="○" c="text-faint" t={grp.label} s={grp.note ?? ""} />
            ) : grp.recent.length === 0 ? (
              <Line g="·" c="text-faint" t={`${grp.label} — none in the window`} s="" />
            ) : (
              grp.recent.map((r, i) => (
                <Line key={i} testid="capacity-routing-row" g={look.g} c={look.c}
                      t={`${r.entitlement_id ?? "no entitlement"} · ${r.item_key}`}
                      s={`“${r.reason}” · ${shortWhen(r.at)}`} />
              ))
            )}
          </Group>
        );
      })}
      {wavesEnded && (
        <Group testid="capacity-routing-waves" verdict="wave_end" k="WAVE END"
               count={wavesEnded.count === null ? "not recorded" : "last full week"}>
          {wavesEnded.count === null ? (
            <Line g="○" c="text-faint" t="Waves ended by capacity" s={wavesEnded.note} />
          ) : (
            <Line g="◒" c="text-[#ff9f5a]" t={`${wavesEnded.count} wave${wavesEnded.count === 1 ? "" : "s"} ended for capacity`} s="no rung had headroom" />
          )}
        </Group>
      )}
    </div>
  );
}
