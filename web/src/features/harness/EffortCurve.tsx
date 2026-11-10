import { useMemo } from "react";

import { cn } from "@/lib/cn";
import type { HarnessCell } from "@/lib/types";
import { leafCells, rowName } from "@/features/harness/PerformanceGrid";
import { accentMap, pairKey } from "@/features/harness/look";

const BANDS = ["S", "M", "L"] as const;

/**
 * One small S / M / L chart per harness : model, side by side, in the pair's own colour.
 * Counts the nested leaves (the rollups would count a two-leaf attempt once per family).
 * A band under the floor is a dashed empty bar that still states its rate.
 */
export function EffortCurve({ cells, floor }: { cells: HarnessCell[]; floor: number }) {
  const rows = useMemo(() => {
    const agg = new Map<string, { vendor: string; model: string; bands: Map<string, { finished: number; signed_off: number }> }>();
    for (const cell of leafCells(cells)) {
      if (cell.kind === "family") continue;
      if (!BANDS.includes(cell.key.size_band as (typeof BANDS)[number])) continue;
      const k = pairKey(cell.key.vendor, cell.key.model);
      const row = agg.get(k) ?? {
        vendor: cell.key.vendor,
        model: cell.key.model,
        bands: new Map(BANDS.map((b) => [b, { finished: 0, signed_off: 0 }])),
      };
      const e = row.bands.get(cell.key.size_band)!;
      e.finished += cell.finished;
      e.signed_off += cell.signed_off;
      agg.set(k, row);
    }
    return [...agg.values()].filter((r) => [...r.bands.values()].some((b) => b.finished > 0));
  }, [cells]);
  const accents = useMemo(() => accentMap(cells.map((c) => pairKey(c.key.vendor, c.key.model))), [cells]);
  if (rows.length === 0) return null;

  return (
    <section data-testid="harness-effort-curves" className="rounded-[12px] border border-line-2 bg-surface px-4 py-3.5">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
        <h2 className="text-lead font-semibold">Effort curve</h2>
        <span className="text-[12px] text-faint">
          Signed-off rate as items get bigger — all functions. A dashed bar is below the floor.
        </span>
      </div>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3.5">
        {rows.map((r) => {
          const accent = accents.get(pairKey(r.vendor, r.model)) ?? "#8b949e";
          return (
            <div key={pairKey(r.vendor, r.model)} data-testid="harness-effort-row">
              <div className="mb-2 flex items-center gap-1.5">
                <span className="size-[7px] flex-none rounded-[2px]" style={{ background: accent }} />
                <span className="truncate font-mono text-small text-fg-2" title={rowName(r)}>{rowName(r)}</span>
              </div>
              <div className="flex h-16 items-end gap-1.5" data-testid="harness-effort-bars">
                {BANDS.map((band) => {
                  const e = r.bands.get(band)!;
                  const below = e.finished < floor;
                  const rate = e.finished > 0 ? e.signed_off / e.finished : null;
                  return (
                    <div
                      key={band}
                      className="flex flex-1 flex-col items-center gap-1"
                      data-testid="harness-effort-band"
                      title={`${band}: ${e.signed_off}/${e.finished} signed off${below ? " — below the floor" : ""}`}
                    >
                      <span className={cn("font-mono text-meta", below ? "text-faint/70" : "text-muted")}>
                        {rate === null ? "—" : Math.round(rate * 100)}
                      </span>
                      <div
                        className={cn("w-full rounded-[4px_4px_2px_2px] border", below ? "border-dashed border-line" : "border-transparent")}
                        style={{
                          height: `${4 + Math.round((rate ?? 0) * 44)}px`,
                          background: below ? "transparent" : `${accent}cc`,
                        }}
                        data-band={band}
                        data-below-floor={below ? "true" : "false"}
                      />
                    </div>
                  );
                })}
              </div>
              <div className="mt-[5px] flex gap-1.5">
                {BANDS.map((b) => (
                  <span key={b} className="flex-1 text-center font-mono text-meta text-faint">{b}</span>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
