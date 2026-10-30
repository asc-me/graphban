import { useMemo, useState } from "react";

import type { HarnessCell, HarnessCost, HarnessReport, HarnessSampling } from "@/lib/types";

type Routing = { function: string; effort_band: string; verdict: string; attempts: number; pick: string | null; evidence: string; fallback: string | null };

const EFFORTS = ["All", "S", "M", "L"] as const;
type Effort = (typeof EFFORTS)[number];

/** One grid cell: every measured cell for a harness row and column, summed over the bands kept. */
interface Agg {
  cells: HarnessCell[];
  finished: number;
  signed_off: number;
  below_floor: boolean;
  skew: boolean;
  probe: number;
}

interface Row {
  key: string;
  vendor: string;
  model: string;
  cells: HarnessCell[];
}

const pct = (r: number | null) => (r === null ? "—" : `${Math.round(r * 100)}%`);

function aggregate(cells: HarnessCell[], floor: number): Agg {
  const finished = cells.reduce((s, c) => s + c.finished, 0);
  const signed_off = cells.reduce((s, c) => s + c.signed_off, 0);
  return {
    cells,
    finished,
    signed_off,
    below_floor: finished < floor,
    skew: cells.some((c) => c.skew !== null),
    probe: cells.reduce((s, c) => s + (c.sampling.probe ?? 0), 0),
  };
}

function tokensLabel(cost: HarnessCost): string {
  return cost.comparable ? `${cost.tokens_per_signed_off} tokens per signed-off item` : cost.reason;
}

/**
 * The design's harness × function grid. Rows are vendor + model exactly as the payload names
 * them (no merging); columns come from the served capability catalog, then any capability a
 * cell carries that the catalog does not list. Below-floor cells keep their numbers.
 */
export function PerformanceGrid({ data, routing }: { data: HarnessReport; routing: Routing[] }) {
  const [effort, setEffort] = useState<Effort>("All");
  const [selected, setSelected] = useState<{ row: string; col: string } | null>(null);

  const { rows, columns, byKey } = useMemo(() => {
    const kept = data.cells.filter((c) => effort === "All" || c.key.size_band === effort);
    const rowMap = new Map<string, Row>();
    const byKey = new Map<string, HarnessCell[]>();
    for (const c of data.cells) {
      const rk = `${c.key.vendor}\u0000${c.key.model}`;
      if (!rowMap.has(rk)) rowMap.set(rk, { key: rk, vendor: c.key.vendor, model: c.key.model, cells: [] });
    }
    for (const c of kept) {
      const rk = `${c.key.vendor}\u0000${c.key.model}`;
      rowMap.get(rk)!.cells.push(c);
      const k = `${rk}\u0001${c.key.capability}`;
      byKey.set(k, [...(byKey.get(k) ?? []), c]);
    }
    // Catalog order first; a capability the catalog does not list still gets a column.
    const catalog = data.capability_set?.leaves ?? [];
    const allCaps = new Set(data.cells.map((c) => c.key.capability));
    const columns = [
      ...catalog.filter((l) => allCaps.has(l)),
      ...[...allCaps].filter((l) => !catalog.includes(l)).sort(),
    ];
    return { rows: [...rowMap.values()], columns, byKey };
  }, [data.cells, data.capability_set, effort]);

  const label = (cap: string) => data.capability_set?.labels[cap];
  const sel = selected ? aggregate(byKey.get(`${selected.row}\u0001${selected.col}`) ?? [], data.floor) : null;
  const selRow = selected ? rows.find((r) => r.key === selected.row) : undefined;

  return (
    <section data-testid="harness-grid-section" className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <span className="text-[13px] font-semibold">Harness × function</span>
        <div role="group" aria-label="Effort" className="ml-auto flex gap-1">
          {EFFORTS.map((e) => (
            <button
              key={e}
              type="button"
              data-testid="harness-effort-filter"
              data-effort={e}
              aria-pressed={effort === e}
              onClick={() => setEffort(e)}
              className={`rounded-[6px] border px-2 py-0.5 font-mono text-small ${
                effort === e ? "border-accent text-fg-2" : "border-control text-muted hover:text-fg-2"
              }`}
            >
              {e}
            </button>
          ))}
        </div>
      </div>

      <div className="flex min-w-0 items-start gap-3">
        <div className="min-w-0 flex-1 overflow-x-auto rounded-[10px] border border-line-2" data-testid="harness-grid-scroll">
          <table className="w-full border-collapse text-left" data-testid="harness-grid">
            <thead>
              <tr className="border-b border-line-2 bg-surface-2">
                <th className="px-3 py-2 font-mono text-[10.5px] font-normal text-faint">Harness · model</th>
                {columns.map((cap) => (
                  <th key={cap} data-testid="harness-grid-col" scope="col" className="min-w-[96px] px-2 py-2 font-mono text-[10.5px] font-normal text-faint">
                    {cap}
                    {label(cap) && label(cap) !== cap && <div className="text-muted">{label(cap)}</div>}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const runs = row.cells.reduce((s, c) => s + c.finished, 0);
                return (
                  <tr key={row.key} data-testid="harness-grid-row" className="border-b border-line-2 last:border-b-0">
                    <th scope="row" className="px-3 py-2 font-normal">
                      <div className="font-mono text-body">{row.vendor}</div>
                      <div className="font-mono text-[10.5px] text-muted">{row.model}</div>
                      <div className="font-mono text-[10.5px] text-faint">{runs} runs</div>
                    </th>
                    {columns.map((cap) => {
                      const cells = byKey.get(`${row.key}\u0001${cap}`);
                      if (!cells || cells.length === 0) {
                        return (
                          <td key={cap} className="px-2 py-2 font-mono text-faint">
                            —
                          </td>
                        );
                      }
                      const a = aggregate(cells, data.floor);
                      const isSel = selected?.row === row.key && selected.col === cap;
                      return (
                        <td key={cap} className="px-1 py-1">
                          <button
                            type="button"
                            data-testid="harness-grid-cell"
                            data-below-floor={a.below_floor ? "true" : "false"}
                            aria-pressed={isSel}
                            aria-label={`${row.vendor} ${row.model} ${cap}: ${a.signed_off} of ${a.finished} signed off${
                              a.below_floor ? ", below the floor" : ""
                            }`}
                            onClick={() => setSelected(isSel ? null : { row: row.key, col: cap })}
                            className={`w-full rounded-[8px] border px-2 py-1.5 text-left ${
                              a.below_floor ? "border-dashed border-control text-faint" : "border-control bg-surface-2"
                            } ${isSel ? "ring-1 ring-accent" : ""}`}
                          >
                            <span className="block text-body font-semibold">{pct(a.finished ? a.signed_off / a.finished : null)}</span>
                            <span className="block font-mono text-[10.5px]">
                              {a.signed_off}/{a.finished}
                            </span>
                            {(a.below_floor || a.skew || a.probe > 0) && (
                              <span className="mt-0.5 flex flex-wrap gap-1 font-mono text-[9px] text-faint">
                                {a.below_floor && <span>floor {data.floor}</span>}
                                {a.skew && <span>skew</span>}
                                {a.probe > 0 && <span>probe</span>}
                              </span>
                            )}
                          </button>
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {selected && sel && selRow && (
          <CellPanel
            row={selRow}
            cap={selected.col}
            capLabel={label(selected.col)}
            agg={sel}
            effort={effort}
            routing={routing.filter((r) => r.function === selected.col && (effort === "All" || r.effort_band === effort))}
            onClose={() => setSelected(null)}
          />
        )}
      </div>
    </section>
  );
}

function CellPanel({
  row, cap, capLabel, agg, effort, routing, onClose,
}: {
  row: Row; cap: string; capLabel?: string; agg: Agg; effort: Effort; routing: Routing[]; onClose: () => void;
}) {
  const rate = agg.finished ? agg.signed_off / agg.finished : null;
  const bands = new Map<string, { finished: number; signed_off: number }>();
  const weeks = new Map<string, { finished: number; signed_off: number }>();
  const mix: HarnessSampling = { first_choice: 0, fallback: 0, explicit: 0, unknown: 0, probe: 0 };
  for (const c of agg.cells) {
    const b = bands.get(c.key.size_band) ?? { finished: 0, signed_off: 0 };
    b.finished += c.finished;
    b.signed_off += c.signed_off;
    bands.set(c.key.size_band, b);
    for (const p of c.series) {
      const w = weeks.get(p.week) ?? { finished: 0, signed_off: 0 };
      w.finished += p.finished;
      w.signed_off += p.signed_off;
      weeks.set(p.week, w);
    }
    mix.first_choice += c.sampling.first_choice;
    mix.fallback += c.sampling.fallback;
    mix.explicit += c.sampling.explicit;
    mix.unknown += c.sampling.unknown;
    mix.probe = (mix.probe ?? 0) + (c.sampling.probe ?? 0);
  }
  // A median or a cost cannot be summed across cells; show it only when one cell carries it.
  const only = agg.cells.length === 1 ? agg.cells[0] : null;
  const weekRows = [...weeks.entries()].sort(([a], [b]) => a.localeCompare(b));

  return (
    <aside data-testid="harness-cell-panel" aria-label="Cell detail" className="w-72 shrink-0 rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
      <div className="flex items-start gap-2">
        <div className="min-w-0">
          <div className="font-mono text-body" data-testid="harness-panel-title">
            {row.vendor}:{row.model}
          </div>
          <div className="font-mono text-[10.5px] text-faint" data-testid="harness-panel-cap">
            {cap}{capLabel && capLabel !== cap ? ` · ${capLabel}` : ""}{effort === "All" ? "" : ` · ${effort}`}
          </div>
        </div>
        <button type="button" aria-label="Close cell detail" onClick={onClose} className="ml-auto text-muted hover:text-fg-2">
          ×
        </button>
      </div>
      <div className={`mt-2 text-[28px] font-semibold ${agg.below_floor ? "text-faint" : ""}`} data-testid="harness-panel-rate">
        {pct(rate)}
      </div>
      <div className="font-mono text-[10.5px] text-faint" data-testid="harness-panel-xy">
        {agg.signed_off}/{agg.finished}
        {agg.below_floor ? " — below the floor" : ""}
      </div>

      {weekRows.length > 0 && (
        <div className="mt-3 flex items-end gap-1" data-testid="harness-panel-weeks">
          {weekRows.map(([w, v]) => (
            <div key={w} className="flex-1" title={`${w}: ${v.signed_off}/${v.finished} signed off`}>
              <div
                className="w-full rounded-[2px] bg-st-done"
                style={{ height: `${4 + Math.round((v.finished ? v.signed_off / v.finished : 0) * 26)}px` }}
              />
              <div className="mt-1 truncate text-center font-mono text-[9px] text-faint">{w.slice(-3)}</div>
            </div>
          ))}
        </div>
      )}

      <dl className="mt-3 flex flex-col gap-1 font-mono text-[10.5px] text-muted">
        <div data-testid="harness-panel-bands">
          by band{" "}
          {[...bands.entries()].map(([b, v]) => `${b} ${v.signed_off}/${v.finished}`).join(" · ")}
        </div>
        <div data-testid="harness-panel-sampling">
          first choice {mix.first_choice} · fallback {mix.fallback} · explicit {mix.explicit} · unknown {mix.unknown}
          {mix.probe ? ` · probe ${mix.probe}` : ""}
        </div>
        {only && only.median_seconds !== null && <div>median time {only.median_seconds}s</div>}
        {only && (
          <div data-testid="harness-panel-cost">
            build {tokensLabel(only.build_cost ?? only.cost)}
            {" · "}review {only.review_cost ? tokensLabel(only.review_cost) : "not reported"}
          </div>
        )}
      </dl>

      {routing.length > 0 && (
        <div className="mt-3" data-testid="harness-panel-told">
          <div className="text-[12px] font-semibold">What gbfleet is told</div>
          {routing.map((r) => (
            <div key={`${r.function}:${r.effort_band}`} className="font-mono text-[10.5px] text-muted">
              {r.effort_band}: {r.pick ?? "no pick"} ({r.verdict}, {r.evidence})
            </div>
          ))}
        </div>
      )}
    </aside>
  );
}
