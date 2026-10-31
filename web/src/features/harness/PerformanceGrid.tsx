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
  /** The family rollup is speaking because no leaf of that family clears the floor. */
  rollup: boolean;
}

interface Row {
  key: string;
  vendor: string;
  model: string;
  runs: number;
}

const pct = (r: number | null) => (r === null ? "—" : `${Math.round(r * 100)}%`);

/**
 * The measured cells under the served shape: each family rollup's nested leaves, plus `other`.
 * A family cell without leaves (an older payload) stands for itself.
 */
export function leafCells(cells: HarnessCell[]): HarnessCell[] {
  return cells.flatMap((c) => (c.kind === "family" && c.leaves && c.leaves.length > 0 ? c.leaves : [c]));
}

function aggregate(cells: HarnessCell[], floor: number, rollup = false): Agg {
  const finished = cells.reduce((s, c) => s + c.finished, 0);
  const signed_off = cells.reduce((s, c) => s + c.signed_off, 0);
  return {
    cells,
    finished,
    signed_off,
    below_floor: finished < floor,
    skew: cells.some((c) => c.skew !== null),
    probe: cells.reduce((s, c) => s + (c.sampling.probe ?? 0), 0),
    rollup,
  };
}

function tokensLabel(cost: HarnessCost): string {
  return cost.comparable ? `${cost.tokens_per_signed_off} tokens per signed-off item` : cost.reason;
}

const SEP = "\u0000";
const rowKeyOf = (c: HarnessCell) => `${c.key.vendor}${SEP}${c.key.model}`;

/**
 * The design's harness × function grid. Rows are vendor + model exactly as the payload names
 * them (no merging). Columns are capability leaves in the served catalog's order. The payload
 * nests leaves under family rollups; a row shows a family's leaves once any of them clears the
 * floor, and the family rollup in that family's column until then. `other` gets its own column.
 * Below-floor cells keep their numbers.
 */
export function PerformanceGrid({ data, routing }: { data: HarnessReport; routing: Routing[] }) {
  const [effort, setEffort] = useState<Effort>("All");
  const [selected, setSelected] = useState<{ row: string; col: string } | null>(null);

  const { rows, columns, grid } = useMemo(() => {
    const keep = (c: HarnessCell) => effort === "All" || c.key.size_band === effort;
    const rowMap = new Map<string, Row>();
    for (const c of data.cells) {
      const rk = rowKeyOf(c);
      if (!rowMap.has(rk)) rowMap.set(rk, { key: rk, vendor: c.key.vendor, model: c.key.model, runs: 0 });
    }
    for (const c of leafCells(data.cells)) {
      if (keep(c)) rowMap.get(rowKeyOf(c))!.runs += c.finished;
    }

    const grid = new Map<string, Agg>();
    const add = (rk: string, col: string, cells: HarnessCell[], rollup = false) =>
      grid.set(`${rk}${SEP}${col}`, aggregate(cells, data.floor, rollup));
    const loose = new Map<string, HarnessCell[]>();
    const families = new Map<string, { rollups: HarnessCell[]; leaves: HarnessCell[] }>();
    for (const c of data.cells) {
      if (!keep(c)) continue;
      const rk = rowKeyOf(c);
      if (c.kind === "family" && c.leaves && c.leaves.length > 0) {
        const fk = `${rk}${SEP}${c.family ?? c.key.capability}`;
        const f = families.get(fk) ?? { rollups: [], leaves: [] };
        f.rollups.push(c);
        f.leaves.push(...c.leaves);
        families.set(fk, f);
      } else {
        const lk = `${rk}${SEP}${c.kind === "other" ? "other" : c.key.capability}`;
        loose.set(lk, [...(loose.get(lk) ?? []), c]);
      }
    }
    for (const [lk, cells] of loose) grid.set(lk, aggregate(cells, data.floor));
    for (const [fk, f] of families) {
      const [vendor, model, family] = fk.split(SEP);
      const rk = `${vendor}${SEP}${model}`;
      const byLeaf = new Map<string, HarnessCell[]>();
      for (const leaf of f.leaves) byLeaf.set(leaf.key.capability, [...(byLeaf.get(leaf.key.capability) ?? []), leaf]);
      const leafClears = [...byLeaf.values()].some((cells) => cells.reduce((s, c) => s + c.finished, 0) >= data.floor);
      if (leafClears) {
        for (const [cap, cells] of byLeaf) add(rk, cap, cells);
      } else {
        add(rk, family, f.rollups, true);
      }
    }

    // Catalog order (each family's rollup column ahead of its leaves); then anything the
    // catalog does not list; `other` last.
    const used = new Set([...grid.keys()].map((k) => k.split(SEP)[2]));
    const catalog = data.capability_set;
    const ordered: string[] = [];
    for (const [family, leaves] of Object.entries(catalog?.families ?? {})) {
      if (family === "other") continue;
      for (const col of [family, ...leaves]) if (used.has(col) && !ordered.includes(col)) ordered.push(col);
    }
    for (const col of catalog?.leaves ?? []) if (used.has(col) && !ordered.includes(col)) ordered.push(col);
    const rest = [...used].filter((c) => !ordered.includes(c) && c !== "other").sort();
    const columns = [...ordered, ...rest, ...(used.has("other") ? ["other"] : [])];
    return { rows: [...rowMap.values()], columns, grid };
  }, [data.cells, data.capability_set, data.floor, effort]);

  const label = (cap: string) => data.capability_set?.labels[cap];
  const sel = selected ? grid.get(`${selected.row}${SEP}${selected.col}`) : undefined;
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
                    {data.capability_set?.families[cap] && cap !== "other" && <div className="text-faint">family</div>}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                return (
                  <tr key={row.key} data-testid="harness-grid-row" className="border-b border-line-2 last:border-b-0">
                    <th scope="row" className="px-3 py-2 font-normal">
                      <div className="font-mono text-body">{row.vendor}</div>
                      <div className="font-mono text-[10.5px] text-muted">{row.model}</div>
                      <div className="font-mono text-[10.5px] text-faint">{row.runs} runs</div>
                    </th>
                    {columns.map((cap) => {
                      const a = grid.get(`${row.key}${SEP}${cap}`);
                      if (!a) {
                        return (
                          <td key={cap} className="px-2 py-2 font-mono text-faint">
                            —
                          </td>
                        );
                      }
                      const isSel = selected?.row === row.key && selected.col === cap;
                      return (
                        <td key={cap} className="px-1 py-1">
                          <button
                            type="button"
                            data-testid="harness-grid-cell"
                            data-below-floor={a.below_floor ? "true" : "false"}
                            data-rollup={a.rollup ? "true" : "false"}
                            aria-pressed={isSel}
                            aria-label={`${row.vendor} ${row.model} ${cap}${a.rollup ? " family rollup" : ""}: ${a.signed_off} of ${a.finished} signed off${
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
                            {(a.rollup || a.below_floor || a.skew || a.probe > 0) && (
                              <span className="mt-0.5 flex flex-wrap gap-1 font-mono text-[9px] text-faint">
                                {a.rollup && <span>family</span>}
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
            {cap}{capLabel && capLabel !== cap ? ` · ${capLabel}` : ""}{agg.rollup ? " · family rollup" : ""}{effort === "All" ? "" : ` · ${effort}`}
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
