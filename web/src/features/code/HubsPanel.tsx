import * as React from "react";

import { cn } from "@/lib/cn";
import type { CodeHub } from "@/lib/types";

/**
 * Hubs — PRD-20 AC-18 (GRPH-480).
 *
 * **Both directions, always.** The AC is *"a file importing forty things must not outrank one
 * forty things import"*, and that failure is invisible when you print one number. Every row
 * carries two bars on one shared scale, so `mcp_server.py` — inbound 5, outbound 18 on the live
 * graph — reads as short-accent/long-grey and its rank stops looking like a mistake.
 *
 * The frontend's own `degrees()` is undirected and is deliberately NOT used here: the ranking
 * comes from the server, which has the direction. Using the local helper would reproduce exactly
 * the measure the AC forbids.
 *
 * **The sort control never says "degree".** Depended-on and depends-on are two different useful
 * questions, and one label for both is how they get conflated.
 */

export type HubSort = "inbound" | "outbound";

interface Props {
  hubs: CodeHub[];
  sort: HubSort;
  onSort: (s: HubSort) => void;
  onPick: (path: string) => void;
  onClose: () => void;
  selected: string | null;
  loading: boolean;
  /** Named so the footnote can say which edges the ranking covers, rather than implying all. */
  edgeTypes: string[];
  allEdgeTypes: string[];
  kindColour: (kind: string) => string;
}

function Row({
  hub, sort, selected, onPick, scale, kindColour,
}: {
  hub: CodeHub;
  sort: HubSort;
  selected: boolean;
  onPick: (p: string) => void;
  scale: number;
  kindColour: (k: string) => string;
}) {
  const lead = sort === "inbound" ? hub.inbound : hub.outbound;
  const trail = sort === "inbound" ? hub.outbound : hub.inbound;
  const slash = hub.path.lastIndexOf("/");
  const dir = slash >= 0 ? hub.path.slice(0, slash + 1) : "";
  const leaf = slash >= 0 ? hub.path.slice(slash + 1) : hub.path;
  const pct = (n: number) => `${scale > 0 ? Math.max(n > 0 ? 2 : 0, (n / scale) * 100) : 0}%`;

  // The row this panel exists to explain: ranked low, and the biggest exporter on the map.
  const inverted = sort === "inbound" && hub.outbound >= 3 * Math.max(hub.inbound, 1);

  return (
    <button
      type="button"
      onClick={() => onPick(hub.path)}
      aria-current={selected ? "true" : undefined}
      aria-label={
        `${hub.path}, ${hub.inbound} depend on it, it depends on ${hub.outbound}` +
        (hub.described ? "" : ", not described")
      }
      className={cn(
        "grid w-full grid-cols-[auto_1fr_auto] items-center gap-2.5 border-l-2 px-3 py-1.5 text-left",
        "focus:outline-none focus-visible:bg-surface-4",
        selected
          ? "border-l-accent bg-surface-4"
          : "border-l-transparent hover:bg-surface-3",
      )}
    >
      <span
        aria-hidden
        className={cn("size-[7px] shrink-0 rounded-full", !hub.described && "border border-dashed")}
        style={
          hub.described
            ? { background: kindColour(hub.kind) }
            : { borderColor: "var(--color-faint)" }
        }
      />
      <span className="min-w-0">
        <span className="block truncate font-mono text-[11.5px] leading-tight">
          <span className="text-faint">{dir}</span>
          <span className="text-fg-2">{leaf}</span>
        </span>
        <span className="mt-1 flex flex-col gap-[2.5px]">
          <span className="block h-[3px] rounded-sm bg-line">
            <span className="block h-full rounded-sm bg-accent" style={{ width: pct(lead) }} />
          </span>
          <span className="block h-[3px] rounded-sm bg-line">
            <span className="block h-full rounded-sm bg-line-3" style={{ width: pct(trail) }} />
          </span>
        </span>
        {inverted && (
          <span className="mt-1 block text-[10.5px] leading-snug text-st-review">
            Imports the most here, and is not a hub. Undirected degree would rank it first.
          </span>
        )}
      </span>
      <span className="shrink-0 text-right font-mono text-[11px] leading-tight">
        <span className="text-accent">{lead}</span>
        <span className="text-faint-2">{sort === "inbound" ? "←" : "→"}</span>
        <br />
        <span className="text-faint">{trail}</span>
        <span className="text-faint-2">{sort === "inbound" ? "→" : "←"}</span>
      </span>
    </button>
  );
}

export function HubsPanel({
  hubs, sort, onSort, onPick, onClose, selected, loading,
  edgeTypes, allEdgeTypes, kindColour,
}: Props) {
  const ranked = React.useMemo(() => {
    const key = (h: CodeHub) => (sort === "inbound" ? h.inbound : h.outbound);
    // Ties broken on path so the list does not reshuffle between identical reads.
    return [...hubs].sort((a, b) => key(b) - key(a) || (a.path < b.path ? -1 : 1));
  }, [hubs, sort]);
  const scale = ranked.length ? Math.max(...ranked.flatMap((h) => [h.inbound, h.outbound])) : 0;
  const filtered = edgeTypes.length > 0 && edgeTypes.length < allEdgeTypes.length;

  return (
    <div className="w-[352px] overflow-hidden rounded-xl border border-line-2 bg-surface-2">
      <div className="border-b border-line px-3 pb-2.5 pt-3">
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-[13px] font-semibold tracking-[-0.1px]">Hubs</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close the hubs panel"
            className="px-1 text-[15px] leading-none text-faint hover:text-fg-2"
          >
            &times;
          </button>
        </div>
        <p className="mt-0.5 text-[11.5px] leading-snug text-muted">
          {sort === "inbound" ? "What breaks if this changes." : "What this would drag with it."}
        </p>
        <div className="mt-2.5 flex w-fit overflow-hidden rounded-lg border border-line-2">
          {(["inbound", "outbound"] as HubSort[]).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => onSort(s)}
              aria-pressed={sort === s}
              className={cn(
                "px-2.5 py-1 text-[11px] first:border-r first:border-line-2",
                sort === s ? "bg-surface-4 text-fg" : "text-muted hover:text-fg-2",
              )}
            >
              {s === "inbound" ? "Depended on" : "Depends on"}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <p className="px-3 py-7 text-center text-[12px] text-muted">Ranking…</p>
      ) : ranked.length === 0 ? (
        <div className="px-4 pb-7 pt-6 text-center">
          <p className="mb-1 text-[12px] text-muted">No edges to rank.</p>
          {/* An empty list must read as NO DATA, never as "no hubs" — this graph covers a
              minority of the tree, so blank is the state a reader will meet first. */}
          <small className="mx-auto block max-w-[30ch] text-[11px] leading-relaxed text-faint">
            Hubs are computed from described relations.
            {filtered
              ? " No edges of the types currently shown — try switching a chip back on."
              : " Nothing here has been described yet; run a describe pass and they appear."}
          </small>
        </div>
      ) : (
        <div className="max-h-[340px] overflow-y-auto py-1">
          {ranked.map((h) => (
            <Row
              key={h.path}
              hub={h}
              sort={sort}
              selected={selected === h.path}
              onPick={onPick}
              scale={scale}
              kindColour={kindColour}
            />
          ))}
        </div>
      )}

      <div className="border-t border-line px-3 pb-3 pt-2 text-[10.5px] leading-relaxed text-faint">
        {sort === "inbound" ? (
          <>
            Ranked by <b className="font-medium text-muted">inbound</b>{" "}
            {filtered ? `${edgeTypes.join(" + ")} ` : ""}edges. A file that imports forty things
            is not a hub; a file forty things import is.
          </>
        ) : (
          <>
            This is <b className="font-medium text-muted">not</b> the hub list — it is what a
            change here would drag with it. A different question, and a useful one.
          </>
        )}
      </div>
    </div>
  );
}
