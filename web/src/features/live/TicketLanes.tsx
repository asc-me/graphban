import { cn } from "@/lib/cn";

import { ageLabel, dotClass, type TicketLane } from "./liveUtils";

export function TicketLanes({ lanes, servedAt }: { lanes: TicketLane[]; servedAt: string }) {
  return (
    <section className="rounded-[11px] border border-line-2 bg-surface-2 p-3.5">
      <h2 className="text-[13px] font-semibold">Tickets in motion</h2>
      <p className="mt-0.5 text-[11.5px] text-muted">One lane per held ticket over the last ten minutes.</p>
      {lanes.length === 0 ? (
        <div className="mt-3 text-[12px] text-muted">No tickets in motion right now.</div>
      ) : (
        <ul className="mt-3 space-y-2" aria-label="ticket lanes">
          {lanes.map((lane) => (
            <li key={lane.id} className="rounded-[9px] border border-line-2 px-2.5 py-2">
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                <span className="font-mono text-[11px] text-muted">{lane.id}</span>
                <span className="truncate text-[12px] text-fg-2">{lane.title}</span>
                {lane.holderLabel && (
                  <span className="text-[10.5px] text-faint">held by {lane.holderLabel}</span>
                )}
              </div>
              <div className="mt-2 flex items-center gap-1" aria-hidden>
                {(lane.dots.length > 0 ? lane.dots : [{ kind: "read" as const, at: null, tool: "" }]).map((d, i) => (
                  <span
                    key={`${lane.id}-${i}`}
                    title={d.tool ? `${d.tool}${d.at ? ` · ${ageLabel(d.at, servedAt)}` : ""}` : lane.status}
                    className={cn("h-2 w-2 rounded-full", dotClass(d.kind))}
                  />
                ))}
              </div>
              <div className="mt-1.5 text-[11px] text-muted">
                {lane.lastEvent ?? `${lane.status} · no events in the last ten minutes`}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
