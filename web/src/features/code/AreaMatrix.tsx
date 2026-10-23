/**
 * Area × area dependency matrix (PRD-52 D3, D4, D7).
 *
 * DOM only: no camera. Rows run from the top layer down to layer 0, then
 * support behind a labelled separator. A healthy import sits above the
 * diagonal. A non-support cell below it is a lower layer reaching up.
 * `tested_by` / `references` on a support row stay grey — those point up
 * by design and are not the warning.
 */

import * as React from "react";

import { areaColor, areasAtScale, canonicalEdges } from "@/lib/graph/areas";
import { buildAreaGraph, deriveLayers, type LayeredArea } from "@/lib/graph/layers";

export const REACHING_UP = "lower layer reaching up — worth a look";
export const CYCLE_NOTE = "cycle";
export const SUPPORT_NOTE = "support: tests/docs/config reference upward by design";
export const EDGE_LIST_CAP = 40;

const NEUTRAL = "#8b949e";
const WARN = "#ff8f8f";
const CYCLE_COLOR = "#c9b8ff";
const TYPE_ORDER = ["imports", "calls", "owns", "tested_by", "references"];

export interface MatrixEdge {
  src: string;
  dst: string;
  type: string;
}

export interface MatrixCellModel {
  row: string;
  col: string;
  rowIndex: number;
  colIndex: number;
  count: number;
  byType: { type: string; count: number }[];
  edges: MatrixEdge[];
  below: boolean;
  tone: "empty" | "neutral" | "warn" | "cycle" | "ok";
  note: string | null;
  /** sqrt(count) / sqrt(max). Zero when the cell is empty. */
  fill: number;
}

export interface MatrixBand {
  tag: string;
  areas: LayeredArea[];
}

export interface MatrixModel {
  order: LayeredArea[];
  bands: MatrixBand[];
  cells: MatrixCellModel[][];
  maxCount: number;
}

export interface MatrixSelection {
  row: string;
  col: string;
}

/** Top layer first, support last. Layer 0 is the sink, so it is not the first row. */
export function matrixOrder(areas: LayeredArea[]): LayeredArea[] {
  const support = areas.filter((a) => a.tag === "support");
  const layered = areas.filter((a) => a.tag !== "support");
  const groups: LayeredArea[][] = [];
  for (const area of layered) {
    const last = groups[groups.length - 1];
    if (!last || last[0].layer !== area.layer) groups.push([area]);
    else last.push(area);
  }
  groups.reverse();
  return [...groups.flat(), ...support];
}

export function cellFill(count: number, max: number): number {
  if (count <= 0 || max <= 0) return 0;
  return Math.sqrt(count) / Math.sqrt(max);
}

function supportEdges(row: LayeredArea, col: LayeredArea, edges: MatrixEdge[]): boolean {
  if (row.tag !== "support" && col.tag !== "support") return false;
  return edges.length > 0 && edges.every((e) => e.type === "tested_by" || e.type === "references");
}

function sameCycle(row: LayeredArea, col: LayeredArea): boolean {
  return row.cycle && col.cycle && row.layer != null && row.layer === col.layer;
}

function bandsOf(order: LayeredArea[]): MatrixBand[] {
  const bands: MatrixBand[] = [];
  for (const area of order) {
    const last = bands[bands.length - 1];
    if (!last || last.tag !== area.tag || last.areas[0].layer !== area.layer) {
      bands.push({ tag: area.tag, areas: [area] });
    } else last.areas.push(area);
  }
  return bands;
}

export function buildMatrix(
  areas: { id: string; members: string[] }[],
  edges: MatrixEdge[],
  enabledTypes?: readonly string[],
): MatrixModel {
  const allowed = enabledTypes ? new Set(enabledTypes) : null;
  const filtered = allowed ? edges.filter((e) => allowed.has(e.type)) : edges;
  const graph = buildAreaGraph(areas, filtered);
  const order = matrixOrder(deriveLayers(graph).areas);
  const index = new Map(order.map((a, i) => [a.id, i]));
  const memberArea = new Map<string, string>();
  for (const area of areas) {
    for (const id of area.members) memberArea.set(id, area.id);
  }

  const raw: MatrixEdge[][][] = order.map(() => order.map(() => []));
  let maxCount = 0;
  for (const e of canonicalEdges(filtered)) {
    if (e.type === "owns") continue;
    const src = memberArea.get(e.src);
    const dst = memberArea.get(e.dst);
    if (!src || !dst || src === dst) continue;
    const i = index.get(src);
    const j = index.get(dst);
    if (i == null || j == null) continue;
    raw[i][j].push(e);
    if (raw[i][j].length > maxCount) maxCount = raw[i][j].length;
  }

  const cells = order.map((rowArea, i) =>
    order.map((colArea, j) => {
      const list = raw[i][j];
      const counts = new Map<string, number>();
      for (const e of list) counts.set(e.type, (counts.get(e.type) ?? 0) + 1);
      const byType = [...counts.entries()]
        .sort((a, b) => {
          const ia = TYPE_ORDER.indexOf(a[0]);
          const ib = TYPE_ORDER.indexOf(b[0]);
          if (ia !== ib) return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
          return a[0] < b[0] ? -1 : 1;
        })
        .map(([type, count]) => ({ type, count }));
      const below = i > j;
      let tone: MatrixCellModel["tone"] = list.length === 0 ? "empty" : "ok";
      let note: string | null = null;
      if (list.length > 0 && sameCycle(rowArea, colArea)) {
        tone = "cycle";
        note = CYCLE_NOTE;
      } else if (list.length > 0 && supportEdges(rowArea, colArea, list)) {
        tone = "neutral";
        note = SUPPORT_NOTE;
      } else if (
        list.length > 0 &&
        below &&
        rowArea.layer != null &&
        colArea.layer != null &&
        rowArea.layer < colArea.layer
      ) {
        tone = "warn";
        note = REACHING_UP;
      }
      return {
        row: rowArea.id,
        col: colArea.id,
        rowIndex: i,
        colIndex: j,
        count: list.length,
        byType,
        edges: list,
        below,
        tone,
        note,
        fill: cellFill(list.length, maxCount),
      };
    }),
  );

  return { order, bands: bandsOf(order), cells, maxCount };
}

export function toneColor(cell: MatrixCellModel): string {
  if (cell.tone === "neutral") return NEUTRAL;
  if (cell.tone === "warn") return WARN;
  if (cell.tone === "cycle") return CYCLE_COLOR;
  return areaColor(cell.row);
}

const CHIPS: { id: "clusters" | "map" | "orbit" | "matrix"; label: string; enabled: boolean }[] = [
  { id: "clusters", label: "Clusters", enabled: true },
  { id: "map", label: "Map", enabled: false },
  { id: "orbit", label: "Orbit", enabled: false },
  { id: "matrix", label: "Matrix", enabled: true },
];

export function LayoutChips({
  layout,
  onLayout,
  returnTo,
}: {
  layout: "clusters" | "matrix";
  onLayout: (id: "clusters" | "matrix") => void;
  returnTo?: { text: string; onClick: () => void } | null;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1">
      <div data-testid="layout-chips" role="group" aria-label="Layout" className="flex gap-1">
        {CHIPS.map((chip) => {
          const pressed = chip.enabled && layout === chip.id;
          return (
            <button
              key={chip.id}
              type="button"
              disabled={!chip.enabled}
              aria-pressed={pressed}
              data-testid={`layout-chip-${chip.id}`}
              onClick={() => {
                if (chip.id === "clusters" || chip.id === "matrix") onLayout(chip.id);
              }}
              className={
                !chip.enabled
                  ? "rounded-lg border border-control bg-surface-2 px-2 py-1 text-[11.5px] text-faint"
                  : pressed
                    ? "rounded-lg border border-control-hover bg-surface-3 px-2 py-1 text-[11.5px] text-fg"
                    : "rounded-lg border border-control bg-surface-2 px-2 py-1 text-[11.5px] text-fg-2"
              }
            >
              {chip.label}
            </button>
          );
        })}
      </div>
      {returnTo && (
        <button
          type="button"
          data-testid="back-to-matrix"
          onClick={returnTo.onClick}
          className="rounded-lg border border-accent/45 bg-accent/10 px-2 py-1 text-[11.5px] text-accent"
        >
          {returnTo.text}
        </button>
      )}
    </div>
  );
}

export function CellInspector({
  cell,
  onShowInGraph,
  onJump,
}: {
  cell: MatrixCellModel;
  onShowInGraph: () => void;
  onJump: (src: string) => void;
}) {
  const shown = cell.edges.slice(0, EDGE_LIST_CAP);
  const more = cell.edges.length - shown.length;
  return (
    <div data-testid="cell-inspector" className="flex flex-col gap-2">
      <div className="font-mono text-[10px] uppercase tracking-wide text-faint">Dependency</div>
      <div data-testid="cell-pair" className="break-all font-mono text-lead text-fg">
        {cell.row} → {cell.col}
      </div>
      <div data-testid="cell-types" className="font-mono text-[11.5px] text-muted">
        {cell.byType.map((t) => `${t.type} ${t.count}`).join(" · ")}
      </div>
      {cell.note && (
        <p data-testid="cell-note" className="text-[12px] leading-relaxed text-muted">
          {cell.note}
        </p>
      )}
      <button
        type="button"
        onClick={onShowInGraph}
        className="h-[26px] self-start rounded-[7px] border border-control bg-surface-2 px-2.5 text-small text-fg-2"
      >
        Show in graph
      </button>
      <div className="flex flex-col gap-0.5">
        {shown.map((e, i) => (
          <button
            key={`${e.src}-${e.dst}-${e.type}-${i}`}
            type="button"
            data-testid="cell-edge"
            onClick={() => onJump(e.src)}
            className="flex items-center gap-2 rounded-md px-1.5 py-1 text-left hover:bg-surface-3"
          >
            <span className="min-w-0 flex-1 truncate font-mono text-small text-fg-2">{e.src}</span>
            <span className="flex-none font-mono text-meta text-muted">{e.type}</span>
          </button>
        ))}
        {more > 0 && (
          <div data-testid="cell-more" className="px-1.5 font-mono text-meta text-faint">
            + {more} more
          </div>
        )}
      </div>
    </div>
  );
}

export function AreaMatrix({
  nodes,
  edges,
  enabledTypes,
  selected,
  onSelect,
  onLayout,
  restoreScroll = 0,
  scrollRef,
}: {
  nodes: { id: string; path: string; kind: string }[];
  edges: MatrixEdge[];
  enabledTypes?: readonly string[];
  selected: MatrixSelection | null;
  onSelect: (cell: MatrixSelection) => void;
  onLayout: (id: "clusters" | "matrix") => void;
  restoreScroll?: number;
  scrollRef?: React.RefObject<HTMLDivElement | null>;
}) {
  const localRef = React.useRef<HTMLDivElement | null>(null);
  const ref = scrollRef ?? localRef;
  const model = React.useMemo(() => {
    const owns = edges.filter((e) => e.type === "owns");
    const scaled = areasAtScale(nodes, owns);
    return buildMatrix(scaled.areas, edges, enabledTypes);
  }, [nodes, edges, enabledTypes]);

  React.useLayoutEffect(() => {
    const el = ref.current;
    if (!el || restoreScroll == null) return;
    el.scrollTop = restoreScroll;
  }, [restoreScroll, ref]);

  let seen = 0;
  return (
    <div data-testid="area-matrix" className="absolute inset-0 flex flex-col bg-surface/40">
      <div className="flex-none p-3">
        <LayoutChips layout="matrix" onLayout={onLayout} />
      </div>
      <div ref={ref} data-testid="matrix-scroll" className="min-h-0 flex-1 overflow-auto px-3 pb-3">
        <table className="border-separate border-spacing-1">
          <thead>
            <tr>
              <th />
              {model.order.map((area) => (
                <th
                  key={area.id}
                  data-testid={`matrix-col-${area.id}`}
                  className="max-w-[72px] truncate font-mono text-[10px] font-normal text-muted"
                  title={area.id}
                >
                  {area.id}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {model.bands.map((band, bi) => (
              <React.Fragment key={`${band.tag}-${band.areas[0]?.id ?? bi}`}>
                {bi > 0 && (
                  <tr data-testid="matrix-separator" data-band={band.tag}>
                    <th
                      colSpan={model.order.length + 1}
                      className="py-1 text-left font-mono text-[10px] uppercase tracking-wide text-faint"
                    >
                      {band.tag}
                    </th>
                  </tr>
                )}
                {band.areas.map((area) => {
                  const i = model.order.findIndex((a) => a.id === area.id);
                  seen += 1;
                  return (
                    <tr key={area.id} data-testid={`matrix-row-${area.id}`}>
                      <th className="max-w-[140px] pr-1 text-left font-normal">
                        <span className="flex items-center gap-1.5">
                          <span
                            data-testid={`matrix-tag-${area.id}`}
                            className="rounded border border-line px-1 font-mono text-[9px] uppercase tracking-wide text-muted"
                          >
                            {area.tag}
                          </span>
                          <span className="truncate font-mono text-[10px] text-fg-2" title={area.id}>
                            {area.id}
                          </span>
                        </span>
                      </th>
                      {model.cells[i].map((cell) => {
                        const selectedOn = selected?.row === cell.row && selected?.col === cell.col;
                        const diag = cell.rowIndex === cell.colIndex ? "diag" : cell.below ? "below" : "above";
                        if (cell.count === 0) {
                          return (
                            <td key={cell.col}>
                              <span
                                data-testid={`matrix-cell-${cell.row}-${cell.col}`}
                                data-row={cell.row}
                                data-col={cell.col}
                                data-zero="true"
                                data-diag={diag}
                                data-tone="empty"
                                className="block h-6 w-6 rounded-sm bg-surface-2/40"
                              />
                            </td>
                          );
                        }
                        return (
                          <td key={cell.col}>
                            <button
                              type="button"
                              data-testid={`matrix-cell-${cell.row}-${cell.col}`}
                              data-row={cell.row}
                              data-col={cell.col}
                              data-diag={diag}
                              data-tone={cell.tone}
                              data-fill={cell.fill.toFixed(4)}
                              aria-pressed={selectedOn}
                              aria-label={`${cell.row} → ${cell.col}, ${cell.count}`}
                              onClick={() => onSelect({ row: cell.row, col: cell.col })}
                              className="block h-6 w-6 rounded-sm border border-transparent"
                              style={{
                                background: toneColor(cell),
                                opacity: Math.max(0.28, cell.fill),
                                outline: selectedOn ? "1px solid #c6f24e" : undefined,
                              }}
                            />
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </React.Fragment>
            ))}
          </tbody>
        </table>
        {seen === 0 && (
          <p className="text-[12px] text-muted">No areas to lay out.</p>
        )}
      </div>
    </div>
  );
}

export function cellFromModel(model: MatrixModel, selected: MatrixSelection | null): MatrixCellModel | null {
  if (!selected) return null;
  const i = model.order.findIndex((a) => a.id === selected.row);
  const j = model.order.findIndex((a) => a.id === selected.col);
  if (i < 0 || j < 0) return null;
  const cell = model.cells[i][j];
  return cell.count > 0 ? cell : null;
}
