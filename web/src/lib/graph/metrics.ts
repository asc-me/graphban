import type { LayoutEdge } from "./layout";

/** Undirected degree per node. Nodes with no edges are present with 0, never absent. */
export function degrees(ids: string[], edges: LayoutEdge[]): Record<string, number> {
  const d: Record<string, number> = {};
  ids.forEach((id) => (d[id] = 0));
  for (const e of edges) {
    if (e.a in d) d[e.a] += 1;
    if (e.b in d) d[e.b] += 1;
  }
  return d;
}

/**
 * The `n` highest-degree ids, ties broken by id so the set is stable across renders.
 *
 * Used for level-of-detail labelling (PRD-20 D2): zoomed out, the only names worth the ink are
 * the hubs. Deterministic for the same reason the layout is — a label that appears and
 * disappears between renders reads as a rendering bug.
 */
export function topByDegree(ids: string[], edges: LayoutEdge[], n: number): Set<string> {
  const d = degrees(ids, edges);
  const ranked = [...ids].sort((a, b) => d[b] - d[a] || (a < b ? -1 : a > b ? 1 : 0));
  return new Set(ranked.slice(0, Math.max(0, n)));
}
