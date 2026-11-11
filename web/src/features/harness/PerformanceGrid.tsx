import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronsLeft, ChevronsRight, X } from "lucide-react";

import { cn } from "@/lib/cn";
import type { HarnessCell, HarnessCost, HarnessReport, HarnessRoutingRow, HarnessSampling } from "@/lib/types";
import { TIER, accentMap, modelFamily, modelVendor, pairKey, pct, tierOf } from "@/features/harness/look";

const EFFORTS = ["All", "S", "M", "L"] as const;
type Effort = (typeof EFFORTS)[number];

/** One grid cell: every measured cell for a row and column, summed over the bands kept. */
interface Agg {
  cells: HarnessCell[];
  finished: number;
  signed_off: number;
  below_floor: boolean;
  skew: boolean;
  probe: number;
  /** A family column: the rollup, with which of the family's leaves clear the floor. */
  leafTicks: { capability: string; finished: number; clears: boolean }[] | null;
}

interface Row {
  key: string;
  vendor: string;
  model: string;
  runs: number;
  /** Filter facets, derived from the names (look.ts). */
  maker: string;
  family: string;
}

interface Column {
  id: string;
  kind: "family" | "leaf";
  /** The family a leaf column sits under; a family column's own id. */
  family: string;
}

const SEP = "\u0000";
const rowKeyOf = (c: HarnessCell) => `${c.key.vendor}${SEP}${c.key.model}`;
const isUnattributed = (r: { vendor: string; model: string }) => !r.vendor && !r.model;
/** "vendor:model", the vendor alone when there is no model, "Unattributed" when neither. */
export const rowName = (r: { vendor: string; model: string }) =>
  isUnattributed(r) ? "Unattributed" : r.model ? (r.vendor ? `${r.vendor}:${r.model}` : r.model) : r.vendor;
const UNATTRIBUTED_HINT = "Finished attempts whose agent declared no vendor or model.";

/** Column widths from the design: a family summary is wider than one function. */
const W_FAMILY = 148;
const W_LEAF = 96;
const W_ROW = 190;

/**
 * The measured cells under the served shape: each family rollup's nested leaves, plus `other`.
 * A family cell without leaves (an older payload) stands for itself.
 */
export function leafCells(cells: HarnessCell[]): HarnessCell[] {
  return cells.flatMap((c) => (c.kind === "family" && c.leaves && c.leaves.length > 0 ? c.leaves : [c]));
}

function aggregate(cells: HarnessCell[], floor: number, leafTicks: Agg["leafTicks"] = null): Agg {
  const finished = cells.reduce((s, c) => s + c.finished, 0);
  const signed_off = cells.reduce((s, c) => s + c.signed_off, 0);
  return {
    cells,
    finished,
    signed_off,
    below_floor: finished < floor,
    skew: cells.some((c) => c.skew !== null),
    probe: cells.reduce((s, c) => s + (c.sampling.probe ?? 0), 0),
    leafTicks,
  };
}

const rateOf = (a: Agg) => (a.finished ? a.signed_off / a.finished : null);

/**
 * The design's harness : model × function grid. Rows are vendor + model exactly as the payload
 * names them. Each capability family is one summary column (the family rollup, with a strip
 * showing which of its functions clear the floor); its » button slides that family's function
 * columns in beside it. Function cells come from the rollup's nested leaves, so every row gets
 * one — a thin one is dashed and tagged `floor`, never dropped. `other` gets its own column.
 * Clicking a cell opens the detail panel over the page.
 */
export function PerformanceGrid({
  data,
  routing,
}: {
  data: HarnessReport;
  routing: HarnessRoutingRow[];
}) {
  const [effort, setEffort] = useState<Effort>("All");
  const [makers, setMakers] = useState<string[]>([]);
  const [harnesses, setHarnesses] = useState<string[]>([]);
  const [families, setFamilies] = useState<string[]>([]);
  const [openFilter, setOpenFilter] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [hideUnder, setHideUnder] = useState(false);
  const [selected, setSelected] = useState<{ row: string; col: string } | null>(null);

  const catalog = data.capability_set;
  const label = (cap: string) => catalog?.labels[cap];

  const { rows, columns, grid } = useMemo(() => {
    const keep = (c: HarnessCell) => effort === "All" || c.key.size_band === effort;
    const rowMap = new Map<string, Row>();
    for (const c of data.cells) {
      const rk = rowKeyOf(c);
      if (!rowMap.has(rk)) {
        rowMap.set(rk, {
          key: rk, vendor: c.key.vendor, model: c.key.model, runs: 0,
          maker: isUnattributed(c.key) ? "Unattributed" : modelVendor(c.key.vendor, c.key.model),
          family: isUnattributed(c.key) ? "Unattributed" : modelFamily(c.key.vendor, c.key.model),
        });
      }
    }
    for (const c of leafCells(data.cells)) {
      if (keep(c)) rowMap.get(rowKeyOf(c))!.runs += c.finished;
    }

    // Bucket the payload: family rollups (with their leaves) and loose cells (other, older shapes).
    const rollups = new Map<string, HarnessCell[]>();
    const leaves = new Map<string, HarnessCell[]>();
    const push = (m: Map<string, HarnessCell[]>, k: string, c: HarnessCell) => m.set(k, [...(m.get(k) ?? []), c]);
    for (const c of data.cells) {
      const rk = rowKeyOf(c);
      if (c.kind === "family") {
        const fam = c.family ?? c.key.capability;
        if (keep(c)) push(rollups, `${rk}${SEP}${fam}`, c);
        for (const leaf of c.leaves ?? []) if (keep(leaf)) push(leaves, `${rk}${SEP}${leaf.key.capability}`, leaf);
        continue;
      }
      if (!keep(c)) continue;
      push(leaves, `${rk}${SEP}${c.kind === "other" ? "other" : c.key.capability}`, c);
    }

    const famOrder = Object.entries(catalog?.families ?? {}).filter(([f, ls]) => f !== "other" && ls.length > 0);
    // A payload without the catalog (an older server) still nests leaves under family rollups:
    // list those families from what was served, so they are not scattered as loose columns.
    for (const c of data.cells) {
      if (c.kind !== "family") continue;
      const fam = c.family ?? c.key.capability;
      const seen = (c.leaves ?? []).map((l) => l.key.capability);
      const entry = famOrder.find(([f]) => f === fam);
      if (!entry) famOrder.push([fam, [...new Set(seen)].sort()]);
      else if (!catalog?.families[fam]) entry[1] = [...new Set([...entry[1], ...seen])].sort();
    }
    const usedFamilies = new Set([...rollups.keys()].map((k) => k.split(SEP)[2]));
    const grid = new Map<string, Agg>();
    for (const row of rowMap.values()) {
      for (const [fam, famLeaves] of famOrder) {
        const cells = rollups.get(`${row.key}${SEP}${fam}`);
        const leafCellsOf = famLeaves.flatMap((l) => leaves.get(`${row.key}${SEP}${l}`) ?? []);
        if (!cells && leafCellsOf.length === 0) continue;
        const ticks = famLeaves.map((capability) => {
          const n = (leaves.get(`${row.key}${SEP}${capability}`) ?? []).reduce((s, c) => s + c.finished, 0);
          return { capability, finished: n, clears: n >= data.floor };
        });
        grid.set(`${row.key}${SEP}${fam}`, aggregate(cells ?? leafCellsOf, data.floor, ticks));
        for (const l of famLeaves) {
          const lc = leaves.get(`${row.key}${SEP}${l}`);
          if (lc) grid.set(`${row.key}${SEP}${l}`, aggregate(lc, data.floor));
        }
      }
      const other = leaves.get(`${row.key}${SEP}other`);
      if (other) grid.set(`${row.key}${SEP}other`, aggregate(other, data.floor));
      // A loose capability the catalog does not list still gets a column of its own.
      for (const [k, cells] of leaves) {
        const [v, m, cap] = k.split(SEP);
        if (`${v}${SEP}${m}` !== row.key || grid.has(k)) continue;
        if (famOrder.some(([, ls]) => ls.includes(cap))) continue;
        grid.set(k, aggregate(cells, data.floor));
      }
    }

    const columns: Column[] = [];
    for (const [fam, famLeaves] of famOrder) {
      if (!usedFamilies.has(fam) && !famLeaves.some((l) => [...grid.keys()].some((k) => k.endsWith(`${SEP}${l}`)))) continue;
      columns.push({ id: fam, kind: "family", family: fam });
      for (const l of famLeaves) columns.push({ id: l, kind: "leaf", family: fam });
    }
    const listed = new Set(columns.map((c) => c.id));
    const loose = [...new Set([...grid.keys()].map((k) => k.split(SEP)[2]))]
      .filter((c) => !listed.has(c) && c !== "other").sort();
    for (const c of loose) columns.push({ id: c, kind: "leaf", family: "" });
    if ([...grid.keys()].some((k) => k.endsWith(`${SEP}other`))) columns.push({ id: "other", kind: "leaf", family: "" });

    const allRows = [...rowMap.values()].sort((a, b) => Number(isUnattributed(a)) - Number(isUnattributed(b)));
    return { rows: allRows, columns, grid };
  }, [data.cells, catalog, data.floor, effort]);

  const accents = useMemo(() => accentMap(rows.map((r) => pairKey(r.vendor, r.model))), [rows]);
  const visibleCols = columns.filter((c) => c.kind === "family" || !c.family || expanded.has(c.family));

  const shownRows = rows.filter(
    (r) =>
      (!makers.length || makers.includes(r.maker)) &&
      (!harnesses.length || harnesses.includes(r.vendor || "Unattributed")) &&
      (!families.length || families.includes(r.family)),
  );
  const cellAt = (row: Row, col: Column) => grid.get(`${row.key}${SEP}${col.id}`);
  const cellSlots = shownRows.length * visibleCols.length;
  const belowCount = shownRows.reduce(
    (n, row) => n + visibleCols.filter((c) => cellAt(row, c)?.below_floor).length,
    0,
  );
  const visibleRows = hideUnder
    ? shownRows.filter((row) => visibleCols.some((c) => {
        const a = cellAt(row, c);
        return !!a && !a.below_floor;
      }))
    : shownRows;
  const hiddenRows = shownRows.length - visibleRows.length;

  const facets = [
    { id: "vendor", label: "Vendor", picked: makers, set: setMakers, opts: [...new Set(rows.map((r) => r.maker))].sort() },
    { id: "harness", label: "Harness", picked: harnesses, set: setHarnesses, opts: [...new Set(rows.map((r) => r.vendor || "Unattributed"))].sort() },
    { id: "family", label: "Family", picked: families, set: setFamilies, opts: [...new Set(rows.map((r) => r.family))].sort() },
  ];
  const anyFilter = facets.some((f) => f.picked.length > 0);

  const familyLeavesOf = (fam: string) => columns.filter((c) => c.kind === "leaf" && c.family === fam).map((c) => c.id);
  const toggleFamily = (fam: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(fam)) next.delete(fam);
      else next.add(fam);
      return next;
    });

  const selRow = selected ? rows.find((r) => r.key === selected.row) : undefined;
  const selCol = selected ? columns.find((c) => c.id === selected.col) : undefined;
  const sel = selRow && selCol ? cellAt(selRow, selCol) : undefined;

  const gridTemplate = `${W_ROW}px ${visibleCols.map((c) => `${c.kind === "family" ? W_FAMILY : W_LEAF}px`).join(" ")} minmax(0,1fr)`;
  const tableMin = W_ROW + visibleCols.reduce((n, c) => n + (c.kind === "family" ? W_FAMILY : W_LEAF), 0);

  const notes = [
    data.coverage && data.coverage.attempts > 0 ? (
      <div key="coverage" data-testid="harness-coverage">
        Coverage {data.coverage.rate === null ? "—" : `${Math.round(data.coverage.rate * 100)}%`}
        {" — "}
        {data.coverage.with_leaf}/{data.coverage.attempts} attempts tagged a leaf. Attempts that
        match none land in <span className="font-mono">other</span>.
      </div>
    ) : null,
    hideUnder ? (
      <div key="floor" data-testid="harness-hide-below-floor-note">
        {`${belowCount} of ${cellSlots} cells are below the ${data.floor}-attempt floor and hidden${
          hiddenRows > 0 ? ` — ${hiddenRows} pair${hiddenRows > 1 ? "s" : ""} with nothing above it removed` : ""
        }. A blank cell means too few attempts, not none.`}
      </div>
    ) : (
      <div key="floor" data-testid="harness-floor-note">
        {belowCount} of {cellSlots} cells are below the {data.floor}-attempt floor. Their rates are
        shown because hiding them would read as having none, not as having too few.
      </div>
    ),
    data.platform === null && data.platform_reason ? (
      <div key="platform" data-testid="harness-no-platform">{data.platform_reason}</div>
    ) : null,
  ].filter(Boolean);

  return (
    <section data-testid="harness-grid-section" className="flex min-w-0 flex-col gap-3">
      <div className="flex flex-wrap gap-2">
        {notes.map((n, i) => (
          <div key={i} className="flex-[1_1_260px] rounded-[10px] border border-line-2 bg-surface-2 px-3 py-[9px] text-[12px] leading-[1.55] text-muted">
            {n}
          </div>
        ))}
      </div>
      {(data.unavailable ?? []).map((row) => (
        <div
          key={`${row.vendor}:${row.model}:${row.capability}:${row.reason}`}
          data-testid="harness-unavailable"
          className="rounded-[10px] border border-dashed border-line px-3 py-[9px] text-[12px] text-faint"
        >
          <span className="font-mono text-fg-2">{row.vendor}{row.model ? `:${row.model}` : ""}</span>
          {" · "}
          <span className="font-mono">{row.capability}</span>
          {" · "}
          <span data-testid="harness-unavailable-reason" className="font-mono">{row.label || row.reason}</span>
          {" — "}greyed because it is {row.reason}, not because it is unmeasured. Control:{" "}
          <span className="font-mono">{row.control}</span>
          {row.drops ? ` · dropped ${row.drops} times` : ""}
        </div>
      ))}

      {openFilter && <div className="fixed inset-0 z-20" onClick={() => setOpenFilter(null)} aria-hidden />}
      <div className="flex flex-wrap items-center gap-2.5" data-testid="harness-grid-toolbar">
        <div className="flex flex-wrap items-center gap-1.5">
          {facets.map((f) => {
            const open = openFilter === f.id;
            const value = !f.picked.length ? "All" : f.picked.length === 1 ? f.picked[0] : `${f.picked.length} selected`;
            return (
              <div key={f.id} className={cn("relative", open ? "z-[21]" : "z-[1]")}>
                <button
                  type="button"
                  data-testid={`harness-filter-${f.id}`}
                  aria-expanded={open}
                  aria-haspopup="true"
                  onClick={() => setOpenFilter(open ? null : f.id)}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-[8px] border bg-surface-2 py-1 pl-2.5 pr-2 text-[11.5px] hover:border-control-hover",
                    f.picked.length ? "border-accent/45 text-fg" : "border-control text-muted",
                  )}
                >
                  <span className="text-faint">{f.label}</span>
                  <span>{value}</span>
                  <ChevronDown size={11} className="text-faint" />
                </button>
                {open && (
                  <div
                    role="group"
                    aria-label={f.label}
                    className="absolute left-0 top-[calc(100%+4px)] flex min-w-[180px] flex-col rounded-[10px] border border-line-2 bg-surface-2 p-1 shadow-[0_8px_24px_rgba(0,0,0,0.45)]"
                  >
                    {f.opts.map((o) => {
                      const on = f.picked.includes(o);
                      return (
                        <button
                          key={o}
                          type="button"
                          role="menuitemcheckbox"
                          aria-checked={on}
                          data-testid="harness-filter-option"
                          onClick={() => f.set(on ? f.picked.filter((x) => x !== o) : [...f.picked, o])}
                          className={cn("flex items-center gap-2 rounded-[6px] px-2 py-1.5 text-left font-mono text-small hover:bg-surface-3", on ? "text-fg" : "text-muted")}
                        >
                          <span
                            className={cn(
                              "flex size-3 flex-none items-center justify-center rounded-[3px] border text-[9px] text-bg",
                              on ? "border-accent bg-accent" : "border-line bg-transparent",
                            )}
                          >
                            {on ? "✓" : ""}
                          </span>
                          {o}
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
          {anyFilter && (
            <button
              type="button"
              data-testid="harness-filter-clear"
              onClick={() => { setMakers([]); setHarnesses([]); setFamilies([]); }}
              className="px-1.5 py-1 text-small text-faint hover:text-fg"
            >
              Clear · {shownRows.length} of {rows.length} pairs
            </button>
          )}
        </div>
        <span className="h-[18px] w-px bg-line-2" aria-hidden />
        <span className="font-mono text-meta tracking-[0.6px] text-faint" id="harness-effort-label">EFFORT</span>
        <div role="group" aria-labelledby="harness-effort-label" className="flex rounded-[8px] border border-line-2 bg-surface-2 p-0.5">
          {EFFORTS.map((e) => (
            <button
              key={e}
              type="button"
              data-testid="harness-effort-filter"
              data-effort={e}
              aria-pressed={effort === e}
              onClick={() => setEffort(e)}
              className={cn("rounded-[6px] px-2.5 py-1 text-[11.5px]", effort === e ? "bg-line-2 text-fg" : "text-muted hover:text-fg-2")}
            >
              {e}
            </button>
          ))}
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={hideUnder}
          data-testid="harness-hide-below-floor"
          onClick={() => setHideUnder((v) => !v)}
          className={cn("inline-flex items-center gap-[7px] px-1.5 py-1 text-[11.5px]", hideUnder ? "text-fg" : "text-muted")}
        >
          <span className={cn("flex h-3.5 w-[26px] rounded-full p-0.5 transition-colors", hideUnder ? "bg-accent" : "bg-line")}>
            <span className={cn("size-2.5 rounded-full bg-bg transition-transform", hideUnder && "translate-x-3")} />
          </span>
          Hide below floor
        </button>
        <span className="flex-1" />
        <GridLegend />
      </div>

      <div className="overflow-x-auto rounded-[12px] border border-line-2 bg-surface" data-testid="harness-grid-scroll">
        <div role="table" aria-label="Harness by function" data-testid="harness-grid" style={{ minWidth: tableMin }}>
          <div role="row" className="grid border-b border-line-2 transition-[grid-template-columns] duration-300" style={{ gridTemplateColumns: gridTemplate }}>
            <div role="columnheader" className="px-3.5 py-2.5 font-mono text-meta tracking-[0.6px] text-faint">HARNESS : MODEL</div>
            {visibleCols.map((col) => {
              if (col.kind === "family") {
                const leaves = familyLeavesOf(col.id);
                const n = leaves.length;
                const open = expanded.has(col.id);
                const name = label(col.id) ?? col.id;
                return (
                  <div
                    key={col.id}
                    role="columnheader"
                    data-testid="harness-grid-col"
                    data-family={col.id}
                    className="flex min-w-0 items-start gap-1.5 border-l border-line-2 bg-surface-2 px-2 py-2.5"
                  >
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="truncate text-[11.5px] font-semibold text-fg" title={`${col.id} · ${name}`}>{col.id} · {name}</span>
                      <span className="whitespace-nowrap font-mono text-[9px] text-faint">
                        {leaves.length ? `${leaves[0]}–${leaves[leaves.length - 1]} · ${n} ${n === 1 ? "leaf" : "leaves"}` : "family"}
                      </span>
                    </span>
                    <button
                      type="button"
                      data-testid="harness-grid-family-expand"
                      data-family={col.id}
                      aria-expanded={open}
                      aria-label={open ? `Collapse ${name}` : `Expand ${name} into ${n} ${n === 1 ? "leaf" : "leaves"}`}
                      title={open ? `Collapse ${name}` : `Expand ${name} into ${n} ${n === 1 ? "leaf" : "leaves"}`}
                      onClick={() => toggleFamily(col.id)}
                      className={cn(
                        "flex size-5 flex-none items-center justify-center rounded-[5px] border bg-surface hover:border-accent hover:text-accent",
                        open ? "border-accent/45 text-accent" : "border-control text-muted",
                      )}
                    >
                      {open ? <ChevronsLeft size={12} /> : <ChevronsRight size={12} />}
                    </button>
                  </div>
                );
              }
              return (
                <div key={col.id} role="columnheader" data-testid="harness-grid-col" data-leaf={col.id} className="min-w-0 overflow-hidden border-l border-line-2/60 px-2 py-2.5">
                  <div className="truncate text-[11.5px] font-medium text-muted-2" title={label(col.id) ?? col.id}>
                    {col.id === "other" ? "Other" : label(col.id) ?? col.id}
                  </div>
                  <div className="whitespace-nowrap font-mono text-[9px] text-faint">{col.id === "other" ? "no leaf" : col.id}</div>
                </div>
              );
            })}
          </div>

          {visibleRows.map((row) => {
            const accent = accents.get(pairKey(row.vendor, row.model));
            return (
              <div
                key={row.key}
                role="row"
                data-testid="harness-grid-row"
                className="grid border-b border-line-2/60 transition-[grid-template-columns] duration-300 last:border-b-0"
                style={{ gridTemplateColumns: gridTemplate }}
              >
                <div role="rowheader" className="flex min-w-0 flex-col justify-center gap-0.5 px-3.5 py-2.5">
                  {isUnattributed(row) ? (
                    <div className="font-mono text-[12px] text-muted" title={UNATTRIBUTED_HINT}>Unattributed</div>
                  ) : (
                    <>
                      <div className="flex items-center gap-[7px]">
                        <span className="size-[7px] flex-none rounded-[2px]" style={{ background: accent }} data-testid="harness-row-accent" />
                        <span className="truncate font-mono text-[12px] text-fg">{row.vendor || row.model}</span>
                      </div>
                      {row.model && row.vendor && (
                        <div className="truncate pl-3.5 font-mono text-[10.5px] text-muted" title={row.model}>{row.model}</div>
                      )}
                    </>
                  )}
                  <div className="pl-3.5 font-mono text-meta text-faint">{row.runs} runs</div>
                </div>
                {visibleCols.map((col) => {
                  const a = cellAt(row, col);
                  const isFam = col.kind === "family";
                  if (!a || (hideUnder && a.below_floor)) {
                    return (
                      <div key={col.id} role="cell" className={cn("min-w-0", isFam && "bg-surface-2/50")}>
                        {!a && !hideUnder && (
                          <div className="m-[5px] flex min-h-16 items-center justify-center rounded-[8px] font-mono text-[10px] text-faint/60" title="No attempts">—</div>
                        )}
                      </div>
                    );
                  }
                  const isSel = selected?.row === row.key && selected.col === col.id;
                  const rate = rateOf(a);
                  const tier = tierOf(rate, a.below_floor);
                  const t = TIER[tier];
                  const name = col.id === "other" ? "other" : label(col.id) ?? col.id;
                  return (
                    <div key={col.id} role="cell" className={cn("min-w-0", isFam && "bg-surface-2/50")}>
                      <button
                        type="button"
                        data-testid="harness-grid-cell"
                        data-below-floor={a.below_floor ? "true" : "false"}
                        data-rollup={isFam ? "true" : "false"}
                        data-tier={tier}
                        aria-pressed={isSel}
                        aria-label={`${[row.vendor, row.model].filter(Boolean).join(" ") || "Unattributed"} ${col.id}${isFam ? " family rollup" : ""}: ${a.signed_off} of ${a.finished} signed off${a.below_floor ? ", below the floor" : ""}`}
                        title={`${rowName(row)} · ${name} · ${a.signed_off}/${a.finished} signed off`}
                        onClick={() => setSelected(isSel ? null : { row: row.key, col: col.id })}
                        className={cn(
                          "m-[5px] flex min-h-16 w-[calc(100%-10px)] flex-col gap-1.5 rounded-[8px] border px-[9px] py-2 text-left transition-colors hover:border-control-hover",
                          t.cell,
                          isSel && "outline outline-1 outline-offset-1 outline-current",
                          isSel && t.text,
                        )}
                      >
                        {isFam ? (
                          <>
                            <span className="flex items-baseline justify-between gap-1.5">
                              <span className={cn("font-mono text-[15px] font-medium tracking-[-0.01em]", t.text)}>{pct(rate)}</span>
                              <span className="whitespace-nowrap font-mono text-[10px] text-faint">{a.signed_off}/{a.finished}</span>
                            </span>
                            <RateBar rate={rate} tier={tier} />
                            {a.leafTicks && a.leafTicks.length > 0 && (
                              <span data-testid="harness-leaf-strip" className="flex flex-col gap-1.5">
                                <span className="flex gap-0.5" aria-hidden="true">
                                  {a.leafTicks.map((tick) => (
                                    <span
                                      key={tick.capability}
                                      data-leaf={tick.capability}
                                      data-clears={tick.clears ? "true" : "false"}
                                      title={`${tick.capability} ${label(tick.capability) ?? ""} · ${tick.finished} attempts${tick.clears ? "" : " — below floor"}`}
                                      className={cn("h-1.5 flex-1 rounded-[1.5px] border", tick.clears ? "border-muted-2 bg-muted-2" : "border-line bg-transparent")}
                                    />
                                  ))}
                                </span>
                                <span className="flex items-baseline gap-[5px] whitespace-nowrap font-mono text-[10px]">
                                  <span className="font-medium text-fg-2">
                                    {a.leafTicks.filter((tick) => tick.clears).length}/{a.leafTicks.length}
                                  </span>{" "}
                                  <span className="text-faint">leaves measured</span>
                                </span>
                              </span>
                            )}
                          </>
                        ) : (
                          <>
                            <span className="flex flex-col gap-px">
                              <span className={cn("font-mono text-body font-medium", t.text)}>{pct(rate)}</span>
                              <span className="whitespace-nowrap font-mono text-[10px] text-faint">{a.signed_off}/{a.finished}</span>
                            </span>
                            <RateBar rate={rate} tier={tier} />
                            <span className="flex min-h-3 gap-1">
                              {a.below_floor && <Flag tone="grey" title={`below the floor — ${a.finished} of ${data.floor} attempts`}>floor</Flag>}
                              {a.skew && <Flag tone="warn" title="sampled by preference">skew</Flag>}
                              {a.probe > 0 && <Flag tone="purple" title="probe samples contributed">probe</Flag>}
                            </span>
                          </>
                        )}
                      </button>
                    </div>
                  );
                })}
              </div>
            );
          })}
          {visibleRows.length === 0 && (
            <div className="px-3.5 py-7 text-center text-body text-faint" data-testid="harness-filter-empty">
              No harness : model pair matches these filters.
            </div>
          )}
        </div>
      </div>

      {selected && sel && selRow && selCol && (
        <CellPanel
          row={selRow}
          col={selCol}
          capLabel={selCol.id === "other" ? "other" : label(selCol.id)}
          agg={sel}
          floor={data.floor}
          effort={effort}
          versions={data.versions}
          routing={routing.filter((r) => r.function === selCol.id && (effort === "All" || r.effort_band === effort))}
          familyLeaves={selCol.kind === "family" ? familyLeavesOf(selCol.id) : []}
          onClose={() => setSelected(null)}
        />
      )}
    </section>
  );
}

function RateBar({ rate, tier }: { rate: number | null; tier: keyof typeof TIER }) {
  return (
    <span className="flex h-[3px] overflow-hidden rounded-[2px] bg-line-2/70" aria-hidden>
      <span className={TIER[tier].bar} style={{ width: `${(rate ?? 0) * 100}%` }} />
    </span>
  );
}

function Flag({ tone, title, children }: { tone: "grey" | "warn" | "purple"; title: string; children: React.ReactNode }) {
  const cls = {
    grey: "border-line-2 text-faint",
    warn: "border-[rgba(240,180,80,0.35)] text-[#f0b450]",
    purple: "border-purple/35 text-purple",
  }[tone];
  return (
    <span title={title} className={cn("rounded-[4px] border px-1 font-mono text-[9px] tracking-[0.3px]", cls)}>
      {children}
    </span>
  );
}

function GridLegend() {
  return (
    <span data-testid="harness-grid-legend" className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-1 font-mono text-[10px] text-faint">
      {(["s", "good", "warn", "bad", "floor"] as const).map((k, i) => (
        <span key={k} className={cn("inline-flex items-center gap-1.5", i > 0 && "ml-1.5")}>
          <span className={cn("size-2.5 rounded-[3px] border", TIER[k].swatch)} />
          {TIER[k].label}
        </span>
      ))}
    </span>
  );
}

function tokensLabel(cost: HarnessCost): string {
  return cost.comparable ? `${cost.tokens_per_signed_off.toLocaleString()} tokens per signed-off item` : cost.reason;
}

/** One value per cell, or each band's value when several cells disagree. */
function perBand(cells: HarnessCell[], f: (c: HarnessCell) => string): string {
  const vals = cells.map(f);
  if (new Set(vals).size <= 1) return vals[0] ?? "not reported";
  return cells.map((c, i) => `${c.key.size_band} ${vals[i]}`).join(" · ");
}

/**
 * The cell detail, slid in over the page from the right: the rate, its floor/skew state, the
 * weekly series, the facts behind it, and what gbfleet is told about that function.
 */
function CellPanel({
  row, col, capLabel, agg, floor, effort, versions, routing, familyLeaves, onClose,
}: {
  row: Row;
  col: Column;
  capLabel?: string;
  agg: Agg;
  floor: number;
  effort: Effort;
  versions: "current" | "all";
  routing: HarnessRoutingRow[];
  familyLeaves: string[];
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const rate = rateOf(agg);
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
  const skew = agg.cells.find((c) => c.skew)?.skew;
  const platform = agg.cells.find((c) => c.platform)?.platform;
  const samples = agg.cells.filter((c) => c.samples).map((c) => c.samples!);
  const util = agg.cells.find((c) => c.utilization)?.utilization;
  const seen = [...new Set(agg.cells.flatMap((c) => c.versions_seen))];
  const notCurrent = agg.cells.some((c) => !c.is_current_version);
  const byProject = new Map<string, { finished: number; signed_off: number }>();
  for (const c of agg.cells) for (const p of c.by_project ?? []) {
    const v = byProject.get(p.project_id) ?? { finished: 0, signed_off: 0 };
    v.finished += p.finished;
    v.signed_off += p.signed_off;
    byProject.set(p.project_id, v);
  }
  const sum = (side: "natural" | "probe") => samples.reduce(
    (a, s) => ({ n: a.n + s[side].n, signed_off: a.signed_off + s[side].signed_off }), { n: 0, signed_off: 0 });

  const facts: { k: string; v: string; testid: string }[] = [
    { k: "by band", v: [...bands.entries()].map(([b, v]) => `${b} ${v.signed_off}/${v.finished}`).join(" · "), testid: "harness-panel-bands" },
    {
      k: "sampling",
      v: `first choice ${mix.first_choice} · fallback ${mix.fallback} · explicit ${mix.explicit} · unknown ${mix.unknown}${mix.probe ? ` · probe ${mix.probe}` : ""}`,
      testid: "harness-panel-sampling",
    },
    { k: "median", v: perBand(agg.cells, (c) => (c.median_seconds !== null ? `${c.median_seconds}s per attempt` : "not reported")), testid: "harness-panel-median" },
    { k: "build cost", v: perBand(agg.cells, (c) => tokensLabel(c.build_cost ?? c.cost)), testid: "harness-panel-cost" },
    { k: "review cost", v: perBand(agg.cells, (c) => (c.review_cost ? tokensLabel(c.review_cost) : "not reported")), testid: "harness-panel-review-cost" },
  ];
  if (platform) {
    facts.push({
      k: "platform",
      v: platform.rate === null
        ? platform.reason
        : `platform average ${Math.round(platform.rate * 100)}% across ${platform.orgs} organisations (n ${platform.n})`,
      testid: "harness-platform",
    });
  }
  if (samples.length) {
    const nat = sum("natural");
    const pr = sum("probe");
    facts.push({
      k: "natural · probe",
      v: `natural ${nat.signed_off}/${nat.n} · probe ${pr.signed_off}/${pr.n}${pr.n > 0 && pr.n < floor ? " — probe below the floor" : ""}`,
      testid: "harness-samples",
    });
  }
  if (util) {
    facts.push({
      k: "turns",
      v: [
        util.turns.reason ?? `median ${util.turns.median} turns / budget ${util.turns.budget_median} (n ${util.turns.reported})`,
        util.budget_hits.reason ?? `budget hit ${Math.round((util.budget_hits.share ?? 0) * 100)}% (n ${util.budget_hits.reported})`,
      ].join(" · "),
      testid: "harness-utilization",
    });
  }
  if (seen.length > 1 || notCurrent) {
    facts.push({ k: "versions", v: `${seen.join(", ")}${notCurrent ? " — includes a build that is not current" : ""}`, testid: "harness-versions-seen" });
  }
  if (byProject.size) {
    facts.push({ k: "projects", v: [...byProject.entries()].map(([p, v]) => `${p} ${v.signed_off}/${v.finished}`).join(" · "), testid: "harness-by-project" });
  }

  const who = rowName(row);
  const where = `${capLabel && capLabel !== col.id ? `${capLabel} (${col.id})` : col.id} · ${effort === "All" ? "all bands" : `band ${effort}`} · ${versions === "current" ? "current version" : "every version"}`;

  const told = toldLines({ who, col, capLabel, routing, familyLeaves, effort });

  return (
    <aside
      data-testid="harness-cell-panel"
      aria-label="Cell detail"
      data-state="open"
      className="gb-sheet fixed bottom-3 right-3 top-16 z-30 flex w-[360px] max-w-[calc(100vw-24px)] flex-col gap-3 overflow-y-auto rounded-[12px] border border-line bg-surface-2 p-4 shadow-[-12px_0_32px_rgba(0,0,0,0.45)]"
    >
      <div className="relative">
        <button
          type="button"
          aria-label="Close cell detail"
          onClick={onClose}
          className="absolute -right-1 -top-1 flex size-[26px] items-center justify-center rounded-[7px] border border-control bg-surface text-muted hover:border-control-hover hover:text-fg"
        >
          <X size={13} />
        </button>
        <div className="font-mono text-meta tracking-[0.6px] text-faint">CELL</div>
        <div className="mt-1 font-mono text-[13px] text-fg" data-testid="harness-panel-title">{who}</div>
        <div className="mt-0.5 pr-8 font-mono text-[10.5px] text-muted" data-testid="harness-panel-cap">
          {where}{col.kind === "family" ? " · family rollup" : ""}
        </div>
      </div>
      <div className="flex items-baseline gap-2.5">
        <span className={cn("text-[30px] font-semibold tracking-[-0.02em]", agg.below_floor ? "text-faint" : "text-fg")} data-testid="harness-panel-rate">
          {pct(rate)}
        </span>
        <span className="font-mono text-small text-muted" data-testid="harness-panel-xy">
          {agg.signed_off}/{agg.finished} signed off
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {agg.below_floor ? (
          <Pill tone="grey" testid="harness-below-floor">below the floor — {agg.finished} of {floor} attempts</Pill>
        ) : skew ? null : (
          <Pill tone="grey">measurement — above the floor</Pill>
        )}
        {skew && (
          <Pill tone="warn" testid="harness-skew">
            sampled by preference — {Math.round(skew.share * 100)}% {skew.reason.replace("_", " ")}
          </Pill>
        )}
      </div>

      {weekRows.length > 0 && (
        <div>
          <div className="mb-1.5 font-mono text-meta tracking-[0.6px] text-faint">WEEKLY</div>
          <div className="flex h-11 items-end gap-1" data-testid="harness-panel-weeks">
            {weekRows.map(([w, v]) => {
              // The same rule the payload applies to a week: under the floor is not a measurement.
              const thin = v.finished < floor;
              return (
                <div key={w} className="flex h-full flex-1 flex-col justify-end" title={`${w}: ${v.signed_off}/${v.finished} signed off${thin ? " — below the floor" : ""}`}>
                  <div
                    className={cn("rounded-[2px]", thin ? "bg-line" : "bg-st-done")}
                    data-thin={thin ? "true" : "false"}
                    style={{ height: `${6 + Math.round((v.finished ? v.signed_off / v.finished : 0) * 36)}px` }}
                  />
                </div>
              );
            })}
          </div>
          <div className="mt-[3px] flex gap-1">
            {weekRows.map(([w]) => (
              <span key={w} className="flex-1 text-center font-mono text-micro text-faint">{w.replace(/^.*W/, "")}</span>
            ))}
          </div>
        </div>
      )}

      <dl className="flex flex-col gap-[5px] border-t border-line-2 pt-2.5">
        {facts.map((f) => (
          <div key={f.k} className="flex gap-2.5 text-[11.5px]" data-testid={f.testid}>
            <dt className="w-24 flex-none pt-px font-mono text-[10px] text-faint">{f.k}</dt>
            <dd className="flex-1 leading-[1.45] text-fg-2">{f.v}</dd>
          </div>
        ))}
      </dl>

      <div className="rounded-[10px] border border-purple/20 bg-[linear-gradient(160deg,rgba(167,139,250,0.06),var(--color-bg))] px-3 py-2.5" data-testid="harness-panel-told">
        <div className="mb-1 font-mono text-meta tracking-[0.6px] text-purple">WHAT GBFLEET IS TOLD</div>
        {told.map((line, i) => (
          <div key={i} className="text-[12px] leading-[1.55] text-fg-2">{line}</div>
        ))}
      </div>
    </aside>
  );
}

function Pill({ tone, testid, children }: { tone: "grey" | "warn"; testid?: string; children: React.ReactNode }) {
  return (
    <span
      data-testid={testid}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10.5px]",
        tone === "warn" ? "border-[rgba(240,180,80,0.35)] bg-[rgba(240,180,80,0.1)] text-[#f0b450]" : "border-line-2 text-faint",
      )}
    >
      {children}
    </span>
  );
}

/**
 * One sentence per band from the served routing rows. The page restates the server's verdict;
 * it never grades. A family column has no routing row of its own — gbfleet routes by function.
 */
function toldLines({
  who, col, capLabel, routing, familyLeaves, effort,
}: {
  who: string;
  col: Column;
  capLabel?: string;
  routing: HarnessRoutingRow[];
  familyLeaves: string[];
  effort: Effort;
}): string[] {
  const name = capLabel ?? col.id;
  if (col.kind === "family") {
    return [
      `Not cited as a family. gbfleet routes by function — expand ${name} to see what it is told for ${familyLeaves.length === 1 ? "its one function" : `each of its ${familyLeaves.length} functions`}.`,
    ];
  }
  if (routing.length === 0) {
    return [`Nothing served for ${name}${effort === "All" ? "" : ` · ${effort}`}: no routing row exists for it in this window.`];
  }
  const order = (b: string) => ({ S: 0, M: 1, L: 2 })[b] ?? 9;
  return [...routing].sort((a, b) => order(a.effort_band) - order(b.effort_band)).map((r) => {
    const band = `${name} · ${r.effort_band}`;
    if (r.pick === who) {
      return r.verdict === "PROBE"
        ? `Named for ${band} as a guess (PROBE) — ${r.evidence}. gbfleet flags any spawn here.`
        : `Cited as ${r.verdict} for ${band} — ${r.evidence}.`;
    }
    return `Not the pick for ${band}: gbfleet is told ${r.verdict} ${r.pick} (${r.evidence}).`;
  });
}
