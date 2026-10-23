/**
 * Shareable code-graph view (PRD-52 D10).
 *
 * The closed set `layout`, `sel`, `depth`, `iso`, `cell` is the namespace: a
 * write deletes and replaces only those keys, so a `ref` or `select` already
 * on the route survives. The camera, pins, trail and area-grouping depth are
 * not in the set. `depth` is the hop-ring control beside Isolate (1–4).
 *
 * Writes use `history.replaceState`, so a change does not push a history entry.
 * The view reads the search string once, on mount.
 */

export const LINKED_NODE_TOAST = "Linked node not in this map";

export const GRAPH_URL_KEYS = ["layout", "sel", "depth", "iso", "cell"] as const;

export type GraphUrlLayout = "clusters" | "map" | "orbit" | "matrix";

const LAYOUTS = new Set<string>(["clusters", "map", "orbit", "matrix"]);

export interface GraphUrlCell {
  row: string;
  col: string;
}

export interface GraphUrlSeed {
  layout: GraphUrlLayout | null;
  sel: string | null;
  /** Hop depth 1–4. Null when the param is absent or not in range. */
  depth: number | null;
  /** Null when `iso` is absent or neither 0 nor 1. */
  iso: boolean | null;
  cell: GraphUrlCell | null;
  /** True when `cell` was present and non-empty, even if it did not parse. */
  cellPresent: boolean;
}

export interface GraphUrlWrite {
  layout: GraphUrlLayout;
  sel: string | null;
  depth: number;
  iso: boolean;
  cell: GraphUrlCell | null;
}

function searchParams(search: string): URLSearchParams {
  const raw = search.startsWith("?") ? search.slice(1) : search;
  return new URLSearchParams(raw);
}

export function readGraphUrl(search: string): GraphUrlSeed {
  const params = searchParams(search);
  const layoutRaw = params.get("layout");
  const layout = layoutRaw && LAYOUTS.has(layoutRaw) ? (layoutRaw as GraphUrlLayout) : null;
  const selRaw = params.get("sel");
  const depthRaw = params.get("depth");
  let depth: number | null = null;
  if (depthRaw != null && depthRaw !== "") {
    const n = Number(depthRaw);
    if (Number.isInteger(n) && n >= 1 && n <= 4) depth = n;
  }
  const isoRaw = params.get("iso");
  const iso = isoRaw === "1" ? true : isoRaw === "0" ? false : null;
  const cellRaw = params.get("cell");
  let cell: GraphUrlCell | null = null;
  if (cellRaw) {
    const cut = cellRaw.indexOf("~");
    if (cut > 0 && cut < cellRaw.length - 1) {
      cell = { row: cellRaw.slice(0, cut), col: cellRaw.slice(cut + 1) };
    }
  }
  return {
    layout,
    sel: selRaw ? selRaw : null,
    depth,
    iso,
    cell,
    cellPresent: !!cellRaw,
  };
}

/** The next query string. Owned keys are rewritten; every other key stays. */
export function writeGraphSearch(current: string, state: GraphUrlWrite): string {
  const params = searchParams(current);
  for (const key of GRAPH_URL_KEYS) params.delete(key);
  if (state.layout !== "clusters") params.set("layout", state.layout);
  if (state.sel) params.set("sel", state.sel);
  if (state.depth !== 1) params.set("depth", String(state.depth));
  if (state.iso) params.set("iso", "1");
  if (state.cell) params.set("cell", `${state.cell.row}~${state.cell.col}`);
  return params.toString();
}

export function replaceGraphUrl(state: GraphUrlWrite): void {
  if (typeof window === "undefined") return;
  const next = writeGraphSearch(window.location.search, state);
  const current = window.location.search.startsWith("?")
    ? window.location.search.slice(1)
    : window.location.search;
  if (next === current) return;
  const href = next
    ? `${window.location.pathname}?${next}${window.location.hash}`
    : `${window.location.pathname}${window.location.hash}`;
  window.history.replaceState(window.history.state, "", href);
}
