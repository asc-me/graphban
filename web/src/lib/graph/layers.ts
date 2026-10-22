/**
 * Derived area layers (PRD-52 D3).
 *
 * Only `imports` and `calls` set layers. An SCC of two or more areas is one
 * layer flagged `cycle`. Layer 0 is areas that depend on nothing; every other
 * layered area is 1 + the longest dependency. Areas with no imports/calls in
 * either direction, only `tested_by` / `references`, sit in `support`.
 * Order inside a layer is lexicographic and carries no meaning.
 */

import { canonicalEdges, type ScaleEdge } from "./areas";

export interface AreaGraphEdge {
  src: string;
  dst: string;
  type: string;
  weight?: number;
}

export interface AreaGraph {
  areas: string[];
  edges: AreaGraphEdge[];
}

export interface LayeredArea {
  id: string;
  /** Null for support. */
  layer: number | null;
  cycle: boolean;
  /** `L<n>`, `cycle`, or `support`. */
  tag: string;
}

export interface DerivedLayers {
  /** Layer ascending, support last, lexicographic within a layer. */
  areas: LayeredArea[];
  byId: Record<string, LayeredArea>;
}

const LAYER_TYPES = new Set(["imports", "calls"]);

function byText(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function tarjan(nodes: string[], deps: Map<string, Set<string>>): string[][] {
  const index = new Map<string, number>();
  const low = new Map<string, number>();
  const stack: string[] = [];
  const on = new Set<string>();
  const sccs: string[][] = [];
  let i = 0;

  function strong(v: string) {
    index.set(v, i);
    low.set(v, i);
    i += 1;
    stack.push(v);
    on.add(v);
    const next = [...(deps.get(v) ?? [])].sort(byText);
    for (const w of next) {
      if (!index.has(w)) {
        strong(w);
        low.set(v, Math.min(low.get(v)!, low.get(w)!));
      } else if (on.has(w)) {
        low.set(v, Math.min(low.get(v)!, index.get(w)!));
      }
    }
    if (low.get(v) === index.get(v)) {
      const scc: string[] = [];
      let w = "";
      do {
        w = stack.pop()!;
        on.delete(w);
        scc.push(w);
      } while (w !== v);
      sccs.push(scc);
    }
  }

  for (const v of [...nodes].sort(byText)) {
    if (!index.has(v)) strong(v);
  }
  return sccs;
}

/**
 * Area graph of typed non-`owns` edges. Intra-area edges are not area edges.
 * Edge order is canonical before the weights are counted.
 */
export function buildAreaGraph(
  areas: { id: string; members: string[] }[],
  edges: ScaleEdge[],
): AreaGraph {
  const memberArea = new Map<string, string>();
  for (const area of areas) {
    if (area.members.length === 0) continue;
    for (const id of area.members) memberArea.set(id, area.id);
  }
  const weight = new Map<string, number>();
  for (const e of canonicalEdges(edges)) {
    if (e.type === "owns") continue;
    const src = memberArea.get(e.src);
    const dst = memberArea.get(e.dst);
    if (!src || !dst || src === dst) continue;
    const key = `${src}\0${dst}\0${e.type}`;
    weight.set(key, (weight.get(key) ?? 0) + 1);
  }
  const ids = [...new Set(areas.filter((a) => a.members.length > 0).map((a) => a.id))].sort(byText);
  const out: AreaGraphEdge[] = [...weight.entries()]
    .sort((a, b) => byText(a[0], b[0]))
    .map(([key, w]) => {
      const [src, dst, type] = key.split("\0");
      return { src, dst, type, weight: w };
    });
  return { areas: ids, edges: out };
}

/**
 * Longest-path layering from the sinks of the imports/calls graph.
 * `areaGraph` areas and edges are sorted on the way in; shuffling them does not move a layer.
 */
export function deriveLayers(areaGraph: AreaGraph): DerivedLayers {
  const areas = [...areaGraph.areas].sort(byText);
  const edges = canonicalEdges(areaGraph.edges);
  const areaSet = new Set(areas);
  const deps = new Map<string, Set<string>>();
  for (const id of areas) deps.set(id, new Set());
  const involved = new Set<string>();

  for (const e of edges) {
    if (!LAYER_TYPES.has(e.type)) continue;
    if (!areaSet.has(e.src) || !areaSet.has(e.dst)) continue;
    involved.add(e.src);
    involved.add(e.dst);
    if (e.src !== e.dst) deps.get(e.src)!.add(e.dst);
  }

  const referenced = new Set<string>();
  for (const e of edges) {
    if (e.type !== "tested_by" && e.type !== "references") continue;
    if (areaSet.has(e.src)) referenced.add(e.src);
    if (areaSet.has(e.dst)) referenced.add(e.dst);
  }
  // No imports/calls, and the edges it does have are tests or references.
  const support = areas.filter((id) => !involved.has(id) && referenced.has(id));
  // Nothing at all: depends on nothing, so it is layer 0 rather than support.
  const alone = areas.filter((id) => !involved.has(id) && !referenced.has(id));
  const layered = areas.filter((id) => involved.has(id));
  const sccs = tarjan(layered, deps);
  const sccOf = new Map<string, number>();
  sccs.forEach((scc, i) => {
    for (const id of scc) sccOf.set(id, i);
  });
  const sccDeps: Set<number>[] = sccs.map(() => new Set());
  for (const [src, dsts] of deps) {
    const si = sccOf.get(src);
    if (si == null) continue;
    for (const dst of dsts) {
      const di = sccOf.get(dst);
      if (di == null || di === si) continue;
      sccDeps[si].add(di);
    }
  }

  const layerOfScc = new Map<number, number>();
  const visiting = new Set<number>();
  function layer(i: number): number {
    const known = layerOfScc.get(i);
    if (known != null) return known;
    if (visiting.has(i)) return 0;
    visiting.add(i);
    let max = 0;
    let any = false;
    for (const d of sccDeps[i] ?? []) {
      any = true;
      max = Math.max(max, layer(d));
    }
    visiting.delete(i);
    const value = any ? max + 1 : 0;
    layerOfScc.set(i, value);
    return value;
  }
  sccs.forEach((_, i) => layer(i));

  const rows: LayeredArea[] = [];
  for (const id of alone) {
    rows.push({ id, layer: 0, cycle: false, tag: "L0" });
  }
  for (let i = 0; i < sccs.length; i++) {
    const cycle = sccs[i].length >= 2;
    const lv = layerOfScc.get(i) ?? 0;
    for (const id of sccs[i]) {
      rows.push({ id, layer: lv, cycle, tag: cycle ? "cycle" : `L${lv}` });
    }
  }
  for (const id of support) {
    rows.push({ id, layer: null, cycle: false, tag: "support" });
  }
  rows.sort((a, b) => {
    const la = a.layer == null ? Number.POSITIVE_INFINITY : a.layer;
    const lb = b.layer == null ? Number.POSITIVE_INFINITY : b.layer;
    if (la !== lb) return la - lb;
    return byText(a.id, b.id);
  });
  const byId: Record<string, LayeredArea> = {};
  for (const row of rows) byId[row.id] = row;
  return { areas: rows, byId };
}
