import { useEffect, useState } from "react";

import { useHarnessRouting } from "@/lib/queries";
import { refusalText } from "./Ladder";

/**
 * Why one item was routed where it was. The verdict, the chosen entitlement and the stored
 * "why" are the server's own words. The per-rung skip trace is not recorded, so it appears only
 * as a PLANNED tag and nothing is drawn under it.
 */
export function RoutingReason({ defaultItem }: { defaultItem?: string | null }) {
  const [typed, setTyped] = useState<string | null>(null);
  const [item, setItem] = useState<string | null>(null);
  useEffect(() => { if (item === null && defaultItem) setItem(defaultItem); }, [defaultItem, item]);
  const { data, error, isError } = useHarnessRouting(item ?? undefined);
  const input = typed ?? item ?? "";

  return (
    <div data-testid="capacity-routing-reason-panel">
      <div className="text-[13px] font-medium">Routing reason</div>
      <form className="mt-1 flex gap-2" onSubmit={(e) => { e.preventDefault(); setItem(input.trim() || null); }}>
        <input aria-label="Item" value={input} onChange={(e) => setTyped(e.target.value)}
               className="rounded-md border border-control bg-transparent px-2 py-0.5 font-mono text-small" />
        <button type="submit" className="rounded-md border border-control px-2 py-0.5 text-small">Show</button>
      </form>
      {isError && <p data-testid="routing-error" className="mt-1 text-body text-muted">{refusalText(error)}</p>}
      {data && (
        <div className="mt-2">
          <div data-testid="routing-head" className="font-mono text-small">
            {data.item_key} · {data.stage} · claimed by {data.claimant ?? "no one"}
            {data.at && ` · ${new Date(data.at).toLocaleString()}`}
          </div>
          {data.latest ? (
            <div className="mt-1 text-body">
              <div className="font-mono text-small">
                <span data-testid="routing-verdict">{data.latest.verdict ?? "unclassed"}</span>{" "}
                <span data-testid="routing-chosen">{data.latest.entitlement_id ?? "no entitlement"}</span>
              </div>
              <div data-testid="routing-why">“{data.latest.why}”</div>
            </div>
          ) : (
            <div data-testid="routing-never" className="mt-1 text-body text-muted">{data.reason}</div>
          )}
          {data.attempts.map((a, i) => (
            <div key={i} data-testid="routing-attempt" className="text-small text-muted">
              {i + 1}. {a.kind} · {a.entitlement_id ?? "no entitlement"} · “{a.reason}”
            </div>
          ))}
          <div className="mt-1 text-small text-muted">
            Skip trace{" "}
            <span data-testid="routing-planned" className="rounded-md border border-line-2 px-1 font-mono text-meta">
              PLANNED · skip trace
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
