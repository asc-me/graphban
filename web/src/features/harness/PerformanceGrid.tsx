import { useMemo, useState } from "react";

import { CardList, filterCards, useCardFilter } from "@/features/harness/Recommendations";
import { useHarnessCards } from "@/lib/queries";
import type { HarnessCard, HarnessCell, HarnessCost, HarnessReport, HarnessSampling } from "@/lib/types";

type Routing = { function: string; effort_band: string; verdict: string; attempts: number; pick: string | null; evidence: string; fallback: string | null };

const EFFORTS = ["All", "S", "M", "L"] as const;
type Effort = (typeof EFFORTS)[number];

/** Signed-off rate band colours from the Nov 9 canvas (above-floor cells only). */
function cellTone(rate: number | null, belowFloor: boolean): string {
  if (belowFloor) return "border-dashed border-control text-faint bg-surface";
  if (rate === null) return "border-control bg-surface-2";
  if (rate > 0.95) return "border-[rgba(95,208,122,0.45)] bg-[rgba(95,208,122,0.18)]";
  if (rate >= 0.8) return "border-[rgba(95,208,122,0.28)] bg-[rgba(95,208,122,0.1)]";
  if (rate >= 0.65) return "border-[rgba(224,179,74,0.35)] bg-[rgba(224,179,74,0.1)]";
  return "border-[rgba(255,107,107,0.35)] bg-[rgba(255,107,107,0.08)]";
}

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
  /** Catalog leaves behind a collapsed family column. A leaf with too few attempts stays in the list. */
  leafTicks: { capability: string; finished: number; clears: boolean }[] | null;
}

interface Row {
  key: string;
  vendor: string;
  model: string;
  runs: number;
}

const isUnattributed = (r: { vendor: string; model: string }) => !r.vendor && !r.model;
/** "vendor:model", the vendor alone when there is no model, "Unattributed" when neither. */
const rowName = (r: { vendor: string; model: string }) =>
  isUnattributed(r) ? "Unattributed" : r.model ? (r.vendor ? `${r.vendor}:${r.model}` : r.model) : r.vendor;
const UNATTRIBUTED_HINT = "Finished attempts whose agent declared no vendor or model.";

const pct = (r: number | null) => (r === null ? "—" : `${Math.round(r * 100)}%`);

/**
 * The measured cells under the served shape: each family rollup's nested leaves, plus `other`.
 * A family cell without leaves (an older payload) stands for itself.
 */
export function leafCells(cells: HarnessCell[]): HarnessCell[] {
  return cells.flatMap((c) => (c.kind === "family" && c.leaves && c.leaves.length > 0 ? c.leaves : [c]));
}

function leafMeasure(
  leaves: HarnessCell[],
  order: string[],
  floor: number,
): { capability: string; finished: number; clears: boolean }[] {
  const finished = new Map<string, number>();
  for (const leaf of leaves) {
    finished.set(leaf.key.capability, (finished.get(leaf.key.capability) ?? 0) + leaf.finished);
  }
  const caps = order.length > 0 ? order : [...finished.keys()];
  return caps.map((capability) => {
    const n = finished.get(capability) ?? 0;
    return { capability, finished: n, clears: n >= floor };
  });
}

function aggregate(
  cells: HarnessCell[],
  floor: number,
  rollup = false,
  leafTicks: Agg["leafTicks"] = null,
): Agg {
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
    leafTicks,
  };
}

function tokensLabel(cost: HarnessCost): string {
  return cost.comparable ? `${cost.tokens_per_signed_off} tokens per signed-off item` : cost.reason;
}

/** A median does not add across cells. One cell states its own; several state each band. */
function medianLine(cells: HarnessCell[]): string {
  const one = (c: HarnessCell) => (c.median_seconds !== null ? `${c.median_seconds}s` : "not reported");
  if (cells.length === 1) return `median time ${one(cells[0])}`;
  return `median ${cells.map((c) => `${c.key.size_band} ${one(c)}`).join(" · ")}`;
}

function costLine(cells: HarnessCell[]): string {
  const build = (c: HarnessCell) => tokensLabel(c.build_cost ?? c.cost);
  const review = (c: HarnessCell) => (c.review_cost ? tokensLabel(c.review_cost) : "not reported");
  if (cells.length === 1) return `build ${build(cells[0])} · review ${review(cells[0])}`;
  return cells
    .map((c) => `${c.key.size_band} build ${build(c)} · review ${review(c)}`)
    .join(" · ");
}

const SEP = "\u0000";
const rowKeyOf = (c: HarnessCell) => `${c.key.vendor}${SEP}${c.key.model}`;

/**
 * The design's harness × function grid. Rows are vendor + model exactly as the payload names
 * them (no merging). Columns are capability leaves in the served catalog's order. The payload
 * nests leaves under family rollups; a row shows a family's leaves once any of them clears the
 * floor, and the family rollup in that family's column until then. `other` gets its own column.
 * Below-floor cells keep their numbers. Family columns stay collapsed until the reader expands
 * them; a collapsed cell shows which of its leaves have cleared the floor.
 */

export function PerformanceGrid({ data, routing, projectId }: { data: HarnessReport; routing: Routing[]; projectId?: string }) {
  const [effort, setEffort] = useState<Effort>("All");
  const [vendor, setVendor] = useState("");
  const [harness, setHarness] = useState("");
  const [family, setFamily] = useState("");
  const [expandedFamilies, setExpandedFamilies] = useState<Set<string>>(() => new Set());
  const [hideUnder, setHideUnder] = useState(false);
  const [showCards, setShowCards] = useState(false);
  const { data: cardData } = useHarnessCards(projectId);
  const [cardFilter] = useCardFilter(projectId);
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
    const add = (
      rk: string,
      col: string,
      cells: HarnessCell[],
      rollup = false,
      leafTicks: Agg["leafTicks"] = null,
    ) => grid.set(`${rk}${SEP}${col}`, aggregate(cells, data.floor, rollup, leafTicks));
    const loose = new Map<string, HarnessCell[]>();
    const families = new Map<string, { rollups: HarnessCell[]; leaves: HarnessCell[] }>();
    for (const c of data.cells) {
      const rk = rowKeyOf(c);
      if (c.kind === "family" && c.leaves && c.leaves.length > 0) {
        const matching = c.leaves.filter(keep);
        if (effort !== "All" && matching.length === 0) continue;
        const fk = `${rk}${SEP}${c.family ?? c.key.capability}`;
        const f = families.get(fk) ?? { rollups: [], leaves: [] };
        f.rollups.push(c);
        f.leaves.push(...matching);
        families.set(fk, f);
        continue;
      }
      if (!keep(c)) continue;
      const lk = `${rk}${SEP}${c.kind === "other" ? "other" : c.key.capability}`;
      loose.set(lk, [...(loose.get(lk) ?? []), c]);
    }
    for (const [lk, cells] of loose) grid.set(lk, aggregate(cells, data.floor));
    for (const [fk, f] of families) {
      const [vendor, model, family] = fk.split(SEP);
      const rk = `${vendor}${SEP}${model}`;
      const byLeaf = new Map<string, HarnessCell[]>();
      for (const leaf of f.leaves) {
        if (!keep(leaf)) continue;
        byLeaf.set(leaf.key.capability, [...(byLeaf.get(leaf.key.capability) ?? []), leaf]);
      }
      const leafClears = [...byLeaf.values()].some((cells) => cells.reduce((s, c) => s + c.finished, 0) >= data.floor);
      if (leafClears && expandedFamilies.has(family)) {
        for (const [cap, cells] of byLeaf) add(rk, cap, cells);
      } else {
        const order = data.capability_set?.families[family] ?? [];
        add(rk, family, f.rollups, true, leafMeasure(f.leaves, order, data.floor));
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
    const allColumns = [...ordered, ...rest, ...(used.has("other") ? ["other"] : [])];
    const inFamily = new Set(family ? [family, ...(data.capability_set?.families[family] ?? [])] : []);
    const columns = family ? allColumns.filter((c) => inFamily.has(c)) : allColumns;
    const allRows = [...rowMap.values()].sort((a, b) => Number(isUnattributed(a)) - Number(isUnattributed(b)));
    return { rows: allRows, columns, grid };
  }, [data.cells, data.capability_set, data.floor, effort, family, expandedFamilies]);

  const vendorOptions = [...new Set(rows.map((r) => r.vendor).filter(Boolean))].sort();
  const shownRows = rows.filter((r) => {
    if (vendor && r.vendor !== vendor) return false;
    if (harness && r.key !== harness) return false;
    return true;
  });
  const familyNames = Object.keys(data.capability_set?.families ?? {}).filter((f) => f !== "other");
  const familyCols = new Set(family ? [family, ...(data.capability_set?.families[family] ?? [])] : []);
  // The grid's own harness/family/effort filter, applied to the cards (rules and state come from the card filter).
  const gridCards: HarnessCard[] = filterCards(cardData?.cards ?? [], { ...cardFilter, harness: "" }).filter((card) =>
    card.cells.some(
      (c) =>
        (!harness || `${c.cell.vendor}${SEP}${c.cell.model}` === harness) &&
        (!family || familyCols.has(c.cell.capability)) &&
        (effort === "All" || c.cell.size_band === effort),
    ),
  );
  const harnessOptions = rows
    .filter((r) => !vendor || r.vendor === vendor)
    .map((r) => ({ value: r.key, label: rowName(r) }));

  const label = (cap: string) => data.capability_set?.labels[cap];
  const familyLeaves = (fam: string) => data.capability_set?.families[fam] ?? [];
  const toggleFamily = (fam: string) =>
    setExpandedFamilies((prev) => {
      const next = new Set(prev);
      if (next.has(fam)) next.delete(fam);
      else next.add(fam);
      return next;
    });
  const sel = selected ? grid.get(`${selected.row}${SEP}${selected.col}`) : undefined;
  const selRow = selected ? rows.find((r) => r.key === selected.row) : undefined;
  const cellSlots = shownRows.length * columns.length;
  const belowCount = shownRows.reduce(
    (n, row) => n + columns.filter((cap) => grid.get(`${row.key}${SEP}${cap}`)?.below_floor).length,
    0,
  );
  const visibleRows = hideUnder
    ? shownRows.filter((row) => columns.some((cap) => {
        const a = grid.get(`${row.key}${SEP}${cap}`);
        return !!a && !a.below_floor;
      }))
    : shownRows;
  const hiddenRows = shownRows.length - visibleRows.length;
  const hiddenPairs = hiddenRows > 0
    ? ` — ${hiddenRows} pair${hiddenRows > 1 ? "s" : ""} with nothing above it removed`
    : "";

  return (
    <section data-testid="harness-grid-section" className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <span className="text-[13px] font-semibold">Harness × function</span>
        <select
          aria-label="Vendor"
          data-testid="harness-grid-vendor"
          value={vendor}
          onChange={(e) => {
            setVendor(e.target.value);
            setHarness("");
          }}
          className="rounded-[8px] border border-control bg-surface px-1.5 py-0.5 text-[11.5px]"
        >
          <option value="">All vendors</option>
          {vendorOptions.map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </select>
        <select
          aria-label="Harness"
          data-testid="harness-grid-harness"
          value={harness}
          onChange={(e) => setHarness(e.target.value)}
          className="rounded-[8px] border border-control bg-surface px-1.5 py-0.5 text-[11.5px]"
        >
          <option value="">All harnesses</option>
          {harnessOptions.map((h) => (
            <option key={h.value} value={h.value}>
              {h.label}
            </option>
          ))}
        </select>
        <select
          aria-label="Capability family"
          data-testid="harness-grid-family"
          value={family}
          onChange={(e) => setFamily(e.target.value)}
          className="rounded-[8px] border border-control bg-surface px-1.5 py-0.5 text-[11.5px]"
        >
          <option value="">All families</option>
          {familyNames.map((f) => (
            <option key={f} value={f}>
              {f}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-1 text-[11.5px] text-muted">
          <input type="checkbox" data-testid="harness-hide-below-floor" checked={hideUnder} onChange={(e) => setHideUnder(e.target.checked)} />
          Hide below-floor
        </label>
        <button
          type="button"
          data-testid="harness-grid-recommendations"
          aria-pressed={showCards}
          onClick={() => setShowCards((v) => !v)}
          className="rounded-[6px] border border-control px-2 py-0.5 text-[11.5px] text-muted hover:text-fg-2"
        >
          Recommendations ({gridCards.length})
        </button>
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

      {!hideUnder && (
        <p className="text-[12px] text-muted" data-testid="harness-floor-note">
          {belowCount} of {cellSlots} cells are below the {data.floor}-attempt floor. Their rates
          are shown because hiding them would read as having none, not as having too few.
        </p>
      )}
      {hideUnder && (
        <p className="text-[12px] text-muted" data-testid="harness-hide-below-floor-note">
          {`${belowCount} of ${cellSlots} cells are below the ${data.floor}-attempt floor and hidden${hiddenPairs}. A blank cell means too few attempts, not none.`}
        </p>
      )}

      <SelectionGridLegend />

      {showCards && (
        <div data-testid="harness-grid-cards">
          {gridCards.length === 0 ? (
            <div className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-body text-muted">
              No recommendations match this filter.
            </div>
          ) : (
            <CardList cards={gridCards} projectId={projectId} />
          )}
        </div>
      )}

      <div className="flex min-w-0 items-start gap-3">
        <div className="min-w-0 flex-1 overflow-x-auto rounded-[10px] border border-line-2" data-testid="harness-grid-scroll">
          <table className="w-full border-collapse text-left" data-testid="harness-grid">
            <thead>
              <tr className="border-b border-line-2 bg-surface-2">
                <th className="px-3 py-2 font-mono text-[10.5px] font-normal text-faint">Harness · model</th>
                {columns.map((cap) => {
                  const isFamily = !!(data.capability_set?.families[cap] && cap !== "other" && familyLeaves(cap).length > 0);
                  const expanded = expandedFamilies.has(cap);
                  return (
                    <th key={cap} data-testid="harness-grid-col" data-family={isFamily ? cap : undefined} scope="col" className="min-w-[96px] px-2 py-2 font-mono text-[10.5px] font-normal text-faint">
                      {isFamily ? (
                        <button
                          type="button"
                          data-testid="harness-grid-family-expand"
                          data-family={cap}
                          aria-expanded={expanded}
                          onClick={() => toggleFamily(cap)}
                          className="text-left hover:text-fg-2"
                        >
                          {cap}
                          {label(cap) && label(cap) !== cap && <div className="text-muted">{label(cap)}</div>}
                          <div className="text-faint">{expanded ? "expanded" : "family · expand"}</div>
                        </button>
                      ) : (
                        <>
                          {cap}
                          {label(cap) && label(cap) !== cap && <div className="text-muted">{label(cap)}</div>}
                        </>
                      )}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {visibleRows.map((row) => {
                return (
                  <tr key={row.key} data-testid="harness-grid-row" className="border-b border-line-2 last:border-b-0">
                    <th scope="row" className="px-3 py-2 font-normal">
                      {isUnattributed(row) ? (
                        <div className="font-mono text-body text-muted" title={UNATTRIBUTED_HINT}>
                          Unattributed
                        </div>
                      ) : (
                        <>
                          <div className="font-mono text-body">{row.vendor}</div>
                          {row.model && <div className="font-mono text-[10.5px] text-muted">{row.model}</div>}
                        </>
                      )}
                      <div className="font-mono text-[10.5px] text-faint">{row.runs} runs</div>
                    </th>
                    {columns.map((cap) => {
                      const a = grid.get(`${row.key}${SEP}${cap}`);
                      if (!a || (hideUnder && a.below_floor)) {
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
                            aria-label={`${[row.vendor, row.model].filter(Boolean).join(" ") || "Unattributed"} ${cap}${a.rollup ? " family rollup" : ""}: ${a.signed_off} of ${a.finished} signed off${
                              a.below_floor ? ", below the floor" : ""
                            }`}
                            onClick={() => setSelected(isSel ? null : { row: row.key, col: cap })}
                            className={`w-full rounded-[8px] border px-2 py-1.5 text-left ${cellTone(a.finished ? a.signed_off / a.finished : null, a.below_floor)} ${
                              isSel ? "ring-1 ring-accent" : ""
                            }`}
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
                            {a.leafTicks && a.leafTicks.length > 0 && (
                              <span data-testid="harness-leaf-strip" className="mt-1 flex items-center gap-1">
                                <span className="flex gap-0.5" aria-hidden="true">
                                  {a.leafTicks.map((tick) => (
                                    <span
                                      key={tick.capability}
                                      data-leaf={tick.capability}
                                      data-clears={tick.clears ? "true" : "false"}
                                      title={`${tick.capability} · ${tick.finished} attempts${tick.clears ? "" : " — below floor"}`}
                                      className={`inline-block h-1.5 w-2 rounded-[1px] border ${
                                        tick.clears ? "border-muted bg-muted" : "border-dashed border-control bg-transparent"
                                      }`}
                                    />
                                  ))}
                                </span>
                                <span className="font-mono text-[9px] text-faint">
                                  {a.leafTicks.filter((tick) => tick.clears).length}/{a.leafTicks.length} leaves measured
                                </span>
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
            cards={(cardData?.cards ?? []).filter((card) =>
              card.cells.some(
                (c) =>
                  c.cell.vendor === selRow.vendor &&
                  c.cell.model === selRow.model &&
                  c.cell.capability === selected.col &&
                  (effort === "All" || c.cell.size_band === effort),
              ),
            )}
            routing={routing.filter((r) => r.function === selected.col && (effort === "All" || r.effort_band === effort))}
            onClose={() => setSelected(null)}
          />
        )}
      </div>
    </section>
  );
}

function SelectionGridLegend() {
  return (
    <div data-testid="harness-grid-legend" className="flex flex-wrap gap-x-3 gap-y-1 text-[10.5px] text-muted">
      <span className="inline-flex items-center gap-1">
        <span className="h-2.5 w-4 rounded border border-[rgba(95,208,122,0.45)] bg-[rgba(95,208,122,0.18)]" /> &gt;95%
      </span>
      <span className="inline-flex items-center gap-1">
        <span className="h-2.5 w-4 rounded border border-[rgba(95,208,122,0.28)] bg-[rgba(95,208,122,0.1)]" /> 80–95%
      </span>
      <span className="inline-flex items-center gap-1">
        <span className="h-2.5 w-4 rounded border border-[rgba(224,179,74,0.35)] bg-[rgba(224,179,74,0.1)]" /> 65–79%
      </span>
      <span className="inline-flex items-center gap-1">
        <span className="h-2.5 w-4 rounded border border-[rgba(255,107,107,0.35)] bg-[rgba(255,107,107,0.08)]" /> &lt;65%
      </span>
      <span className="inline-flex items-center gap-1">
        <span className="h-2.5 w-4 rounded border border-dashed border-control bg-surface" /> below floor
      </span>
    </div>
  );
}

function CellPanel({
  row, cap, capLabel, agg, effort, routing, cards, onClose,
}: {
  row: Row; cap: string; capLabel?: string; agg: Agg; effort: Effort; routing: Routing[]; cards: HarnessCard[]; onClose: () => void;
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
  const weekRows = [...weeks.entries()].sort(([a], [b]) => a.localeCompare(b));

  return (
    <aside data-testid="harness-cell-panel" aria-label="Cell detail" className="w-72 shrink-0 rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
      <div className="flex items-start gap-2">
        <div className="min-w-0">
          <div className="font-mono text-body" data-testid="harness-panel-title">
            {rowName(row)}
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
        <div data-testid="harness-panel-median">{medianLine(agg.cells)}</div>
        <div data-testid="harness-panel-cost">{costLine(agg.cells)}</div>
      </dl>

      <div className="mt-3" data-testid="harness-panel-change">
        <div className="text-[12px] font-semibold">What to change</div>
        {cards.length === 0 ? (
          <div className="font-mono text-[10.5px] text-muted">No rule fired on this cell</div>
        ) : (
          cards.map((card) => (
            <div key={card.key} data-testid="harness-panel-change-card" className="mt-1 font-mono text-[10.5px] text-muted">
              <div>
                {card.rule} · {card.title}
              </div>
              <div className="break-all text-faint">
                apply by hand: {String(card.draft.where ?? "")}
                {typeof card.draft.evidence_line === "string" ? ` — ${card.draft.evidence_line}` : ""}
              </div>
            </div>
          ))
        )}
      </div>

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
