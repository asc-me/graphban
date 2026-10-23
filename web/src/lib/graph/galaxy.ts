import type { LayoutEdge, Pos } from "./layout";
import { adjacency, degrees } from "./metrics";

/**
 * What the within-repo galaxy view (PRD-20 D9) left behind after PRD-52 D8.
 *
 * `collapse`, `enterComponent`, `superRadius` and `worthCollapsing` are gone. Above
 * `DETAIL_BUDGET` the code graph draws every node on the canvas; it does not collapse
 * components into super-nodes.
 *
 * Still here, because something else reads them:
 * - `DETAIL_BUDGET` — the mode switch in `CodeGraphView`.
 * - `componentsOf` — the client mirror of `code_graph.components()` over the edges
 *   currently drawn. The view no longer calls it. The suite pins it to the same fixture
 *   as the server so the two pictures cannot drift unnoticed.
 * - `convexHull` / `hullPath` — SVG-mode regions. They stay in this file because it is
 *   not empty; D8 moves them to `hulls.ts` only if it would be.
 * - `orderWithin` — degree order inside one set of ids.
 */

/** Above this many nodes the flat SVG view stops and canvas mode takes over. */
export const DETAIL_BUDGET = 800;

export interface Component {
  /** Highest-inbound member — the label a component wears. */
  anchor: string;
  members: string[];
}

/**
 * Connected components over the given edges, ordered largest-first.
 *
 * **This mirrors `code_graph.components()` on the server, deliberately and with a reason.** The
 * server answers for agents over the STORED graph; this answers for the live view over the
 * edges currently DRAWN, which the server cannot know because edge-type filtering is
 * client-side and instant. A round trip per filter chip would undo D1's whole point. Both
 * implementations follow the same ordering contract and the same fixture is asserted in both
 * suites, so a divergence surfaces as a failing test rather than as two different pictures.
 *
 * Deterministic: ids walked in sorted order, neighbours visited sorted, members sorted, and
 * anchor ties broken on the id. A structural overlay that reshuffled between identical reads
 * would give away exactly the stability D1 protects.
 */
export function componentsOf(ids: string[], edges: LayoutEdge[]): Component[] {
  const sorted = [...ids].sort();
  const adj = adjacency(sorted, edges);
  for (const k of Object.keys(adj)) adj[k] = [...adj[k]].sort();

  const inbound: Record<string, number> = {};
  sorted.forEach((id) => (inbound[id] = 0));
  for (const e of edges) if (e.b in inbound) inbound[e.b] += 1;

  const seen = new Set<string>();
  const found: string[][] = [];
  for (const start of sorted) {
    if (seen.has(start)) continue;
    seen.add(start);
    const stack = [start];
    const member: string[] = [];
    while (stack.length) {
      const cur = stack.pop()!;
      member.push(cur);
      for (const nb of adj[cur] ?? []) {
        if (!seen.has(nb)) {
          seen.add(nb);
          stack.push(nb);
        }
      }
    }
    found.push(member.sort());
  }

  found.sort((a, b) => b.length - a.length || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  return found.map((members) => ({
    anchor: [...members].sort((a, b) => inbound[b] - inbound[a] || (a < b ? -1 : a > b ? 1 : 0))[0],
    members,
  }));
}

/**
 * Convex hull by monotone chain, with padding — a region around a set of points.
 *
 * Convex rather than an alpha shape, per the grill: deterministic, cheap, and a working region
 * should read as one blob rather than a concave outline tracing individual nodes. Returns an
 * empty list below three points, since two points are a line and one is a dot — neither is a
 * region, and drawing a hull for them would claim an area that is not there.
 */
export function convexHull(points: Pos[], padding = 0): Pos[] {
  if (points.length < 3) return [];
  const pts = [...points].sort((a, b) => a.x - b.x || a.y - b.y);
  const cross = (o: Pos, a: Pos, b: Pos) =>
    (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);

  const lower: Pos[] = [];
  for (const p of pts) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) {
      lower.pop();
    }
    lower.push(p);
  }
  const upper: Pos[] = [];
  for (let i = pts.length - 1; i >= 0; i--) {
    const p = pts[i];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) {
      upper.pop();
    }
    upper.push(p);
  }
  const hull = [...lower.slice(0, -1), ...upper.slice(0, -1)];
  // Collinear input collapses to fewer than three distinct corners — still not a region.
  if (hull.length < 3) return [];
  if (!padding) return hull;

  const cx = hull.reduce((s, p) => s + p.x, 0) / hull.length;
  const cy = hull.reduce((s, p) => s + p.y, 0) / hull.length;
  return hull.map((p) => {
    const d = Math.hypot(p.x - cx, p.y - cy) || 1;
    return { x: p.x + ((p.x - cx) / d) * padding, y: p.y + ((p.y - cy) / d) * padding };
  });
}

/** An SVG path for a hull, or "" when there is no region to draw. */
export function hullPath(points: Pos[]): string {
  if (points.length < 3) return "";
  return `${points.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ")} Z`;
}

/** Degree order within one set of ids — reused for tab order and label level-of-detail. */
export function orderWithin(ids: string[], edges: LayoutEdge[]): string[] {
  const d = degrees(ids, edges);
  return [...ids].sort((a, b) => d[b] - d[a] || (a < b ? -1 : a > b ? 1 : 0));
}
