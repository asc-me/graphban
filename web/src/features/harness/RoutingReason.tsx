import { useEffect, useState } from "react";

import { cn } from "@/lib/cn";
import { useHarnessRouting } from "@/lib/queries";
import type { HarnessRoutingAttempt } from "@/lib/types";
import { shortWhen } from "./capacityLook";
import { refusalText } from "./Ladder";

/** What became of one recorded decision, in the router's own terms. */
function outcome(a: HarnessRoutingAttempt): { word: string; tone: string } {
  if (a.kind === "claim") {
    return a.entitlement_id
      ? { word: a.matched === false ? "CLAIMED · OFF RESERVATION" : "CLAIMED", tone: "text-st-done" }
      : { word: "NEXT RUNG", tone: "text-muted" };
  }
  if (a.kind === "reservation") {
    return a.ended_reason ? { word: "RESERVATION ENDED", tone: "text-[#ff9f5a]" } : { word: "HELD", tone: "text-st-done" };
  }
  return { word: a.kind.toUpperCase(), tone: "text-muted" };
}

/**
 * Why one item was routed where it was. The verdict, the chosen entitlement and the stored
 * "why" are the server's own words, and so is each recorded decision below them. The per-rung
 * skip trace is not recorded, so it appears only as a PLANNED tag and nothing is drawn under it.
 */
export function RoutingReason({ defaultItem }: { defaultItem?: string | null }) {
  const [typed, setTyped] = useState<string | null>(null);
  const [item, setItem] = useState<string | null>(null);
  useEffect(() => { if (item === null && defaultItem) setItem(defaultItem); }, [defaultItem, item]);
  const { data, error, isError } = useHarnessRouting(item ?? undefined);
  const input = typed ?? item ?? "";
  const chosen = data?.latest?.entitlement_id ?? null;

  return (
    <div data-testid="capacity-routing-reason-panel" className="flex flex-col rounded-[12px] border border-line-2 bg-surface">
      <div className="flex flex-wrap items-center gap-2.5 border-b border-line-2/70 px-4 py-3">
        <h3 className="text-[13.5px] font-semibold">Routing reason</h3>
        {data && (
          <span data-testid="routing-head" className="font-mono text-[10.5px] text-muted">
            {data.item_key} · {data.stage} · claimed by {data.claimant ?? "no one"}
            {data.at && ` · ${shortWhen(data.at)}`}
          </span>
        )}
        <span className="flex-1" />
        <form className="flex gap-1.5" onSubmit={(e) => { e.preventDefault(); setItem(input.trim() || null); }}>
          <input aria-label="Item" value={input} placeholder="GRPH-123" onChange={(e) => setTyped(e.target.value)}
                 className="h-7 w-[120px] rounded-[7px] border border-control bg-transparent px-2 font-mono text-small text-fg placeholder:text-faint" />
          <button type="submit" className="h-7 rounded-[7px] border border-control px-2.5 text-small text-fg-2 hover:bg-surface-3">Show</button>
        </form>
      </div>
      {!item && !isError && (
        <p className="px-4 py-3 text-[12px] text-muted">No routed claim in the window to default to. Name an item to see why it went where it did.</p>
      )}
      {isError && <p data-testid="routing-error" className="px-4 py-3 text-body text-muted">{refusalText(error)}</p>}
      {data && (
        <>
          {data.latest ? (
            <div className={cn("flex flex-col gap-[5px] px-4 py-3", chosen ? "bg-[rgba(95,208,122,0.05)]" : "bg-[rgba(255,159,90,0.06)]")}>
              <span className="font-mono text-meta tracking-[0.6px] text-muted">
                <span data-testid="routing-verdict">{chosen ? "CHOSEN ENTITLEMENT" : (data.latest.verdict ?? "unclassed").toUpperCase()}</span> · SERVER'S REASON
              </span>
              <span data-testid="routing-chosen" className="font-mono text-[13px] text-fg">{chosen ?? "no entitlement"}</span>
              <span data-testid="routing-why" className="font-mono text-[12px] leading-[1.5] text-fg-2">“{data.latest.why}”</span>
            </div>
          ) : (
            <div data-testid="routing-never" className="px-4 py-3 text-body text-muted">{data.reason}</div>
          )}
          {data.attempts.map((a, i) => {
            const o = outcome(a);
            return (
              <div key={i} data-testid="routing-attempt" className="grid grid-cols-[28px_130px_minmax(0,1fr)_auto] items-start gap-3 border-t border-line-2/70 px-4 py-2.5 text-[12px]">
                <span className="text-center font-mono text-muted">{i + 1}</span>
                <span className="font-mono text-fg">{a.kind}</span>
                <span className="flex min-w-0 flex-col gap-[3px]">
                  <span className="leading-[1.45] text-fg-2">{a.entitlement_id ?? "no entitlement"}</span>
                  <span className="font-mono text-small text-muted-2">“{a.reason}”{a.ended_reason ? ` · ended: ${a.ended_reason}` : ""}</span>
                </span>
                <span className={cn("justify-self-end whitespace-nowrap font-mono text-meta font-semibold tracking-[0.6px]", o.tone)}>{o.word}</span>
              </div>
            );
          })}
          <div className="flex items-center gap-2 border-t border-line-2/70 px-4 py-2.5 text-small text-muted">
            Rungs skipped before the choice
            <span data-testid="routing-planned" className="rounded-[4px] border border-dashed border-line px-[5px] font-mono text-[9px] tracking-[0.5px] text-muted">
              PLANNED · skip trace
            </span>
          </div>
        </>
      )}
    </div>
  );
}
