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

/** Undirected adjacency. Every id is present, so a lookup never returns undefined. */
export function adjacency(ids: string[], edges: LayoutEdge[]): Record<string, string[]> {
  const adj: Record<string, string[]> = {};
  ids.forEach((id) => (adj[id] = []));
  for (const e of edges) {
    if (e.a in adj && e.b in adj) {
      adj[e.a].push(e.b);
      adj[e.b].push(e.a);
    }
  }
  return adj;
}

/**
 * Every node within `depth` hops of `from`, inclusive of `from` itself (PRD-20 D3).
 *
 * A breadth-first walk rather than a one-hop scan, because "expand the highlight by one ring"
 * has to keep working at ring two and three — the previous code hard-coded a single hop and
 * had nowhere to grow.
 */
export function withinHops(
  ids: string[],
  edges: LayoutEdge[],
  from: string,
  depth: number,
): Set<string> {
  const seen = new Set<string>();
  if (!ids.includes(from)) return seen;
  const adj = adjacency(ids, edges);
  seen.add(from);
  let frontier = [from];
  for (let d = 0; d < Math.max(0, depth); d++) {
    const next: string[] = [];
    for (const id of frontier) {
      for (const nb of adj[id] ?? []) {
        if (seen.has(nb)) continue;
        seen.add(nb);
        next.push(nb);
      }
    }
    if (next.length === 0) break;
    frontier = next;
  }
  return seen;
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
