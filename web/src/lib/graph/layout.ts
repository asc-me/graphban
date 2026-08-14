/**
 * The one force-directed layout, shared by every graph view (PRD-20 D1).
 *
 * Before this file there were two byte-identical copies — `CodeGraphView.tsx:44` and
 * `LinksGraphView.tsx:25` — differing only in whether an edge's endpoints were named
 * `src`/`dst` or `a`/`b`. Callers now adapt to `LayoutEdge` at the call site, which is the
 * whole of what the duplication was buying.
 *
 * **Deterministic by construction.** No `Math.random()`: the seed is a circle in index order
 * and every force is a pure function of position. The same inputs give the same output on
 * every render, which is the property the views rely on to keep a node where the user last
 * saw it — and the reason this is hand-written rather than delegated to d3-force.
 */

export interface Pos {
  x: number;
  y: number;
}

/** An undirected pair for layout purposes. Direction matters for drawing, never for physics. */
export interface LayoutEdge {
  a: string;
  b: string;
}

export interface LayoutOpts {
  width: number;
  height: number;
  /** Defaults to `iterationsFor(ids.length)`. Pass explicitly only to pin it in a test. */
  iterations?: number;
}

// Physics, carried over unchanged from the two originals so the extraction moves no node.
const REPULSION = 26000;
const REST = 150;
const SPRING = 0.06;
const CENTER_PULL = 0.02;
const STEP = 0.6;
const MAX_STEP = 14;

/** The iteration count the originals used, and still the count for any graph we render today. */
export const BASE_ITERATIONS = 300;

/**
 * How many relaxation passes to run for `n` nodes.
 *
 * Each pass is O(n²) in the repulsion loop, so a fixed 300 is ~37M pair-interactions at 500
 * nodes and ~96M at 800 — seconds, not milliseconds, which busts D1's 1.5s budget well before
 * the node count becomes unreasonable. Above the knee we trade relaxation for latency and hold
 * total work roughly constant.
 *
 * Below `KNEE` the count is exactly `BASE_ITERATIONS`, so every graph we actually render today
 * (the live code graph is ~123 nodes) lays out identically to before this file existed. That
 * matters: a layout change and a refactor landing in one commit is a change nobody can review.
 *
 * **Where this stops working, and why that is the point.** `MIN_ITERATIONS` and the flat-work
 * budget are in direct conflict, and the floor wins from about 821 nodes up: past there the
 * count is pinned at 40 and total work grows as O(n²) again. That crossover is not a tuning
 * accident — it is the real ceiling of an O(n²) layout, and it is exactly where D1's 800-node
 * budget comes from. Above it the answer is D9's galaxy view, which lays out *components*
 * rather than nodes, or a Barnes-Hut inner loop. It is never more tuning here.
 */
const KNEE = 300;
const MIN_ITERATIONS = 40;
export function iterationsFor(n: number): number {
  if (n <= KNEE) return BASE_ITERATIONS;
  const budget = BASE_ITERATIONS * KNEE * KNEE;
  return Math.max(MIN_ITERATIONS, Math.round(budget / (n * n)));
}

export function computeLayout(
  ids: string[],
  edges: LayoutEdge[],
  opts: LayoutOpts,
): Record<string, Pos> {
  const { width, height } = opts;
  const iterations = opts.iterations ?? iterationsFor(ids.length);
  const n = ids.length;
  const cx = width / 2;
  const cy = height / 2;
  const r = Math.min(width, height) / 2.6;

  const pos: Record<string, Pos> = {};
  ids.forEach((id, i) => {
    const angle = (2 * Math.PI * i) / Math.max(1, n);
    pos[id] = { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) };
  });

  for (let iter = 0; iter < iterations; iter++) {
    const disp: Record<string, Pos> = {};
    ids.forEach((id) => (disp[id] = { x: 0, y: 0 }));

    // Repulsion between all pairs.
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const u = ids[i];
        const v = ids[j];
        let dx = pos[u].x - pos[v].x;
        let dy = pos[u].y - pos[v].y;
        const d2 = dx * dx + dy * dy || 0.01;
        const f = REPULSION / d2;
        const d = Math.sqrt(d2);
        dx /= d;
        dy /= d;
        disp[u].x += dx * f;
        disp[u].y += dy * f;
        disp[v].x -= dx * f;
        disp[v].y -= dy * f;
      }
    }

    // Springs along edges.
    for (const e of edges) {
      if (!pos[e.a] || !pos[e.b]) continue;
      let dx = pos[e.b].x - pos[e.a].x;
      let dy = pos[e.b].y - pos[e.a].y;
      const d = Math.hypot(dx, dy) || 0.01;
      const f = (d - REST) * SPRING;
      dx = (dx / d) * f;
      dy = (dy / d) * f;
      disp[e.a].x += dx;
      disp[e.a].y += dy;
      disp[e.b].x -= dx;
      disp[e.b].y -= dy;
    }

    // Center pull + integrate (capped step).
    for (const id of ids) {
      disp[id].x += (cx - pos[id].x) * CENTER_PULL;
      disp[id].y += (cy - pos[id].y) * CENTER_PULL;
      pos[id].x += Math.max(-MAX_STEP, Math.min(MAX_STEP, disp[id].x * STEP));
      pos[id].y += Math.max(-MAX_STEP, Math.min(MAX_STEP, disp[id].y * STEP));
    }
  }

  return pos;
}

/**
 * A stable key for a node set + edge set.
 *
 * Layout re-runs when this changes and not otherwise, which is what makes an edge-type filter
 * a *drawing* concern rather than a re-layout (PRD-20 AC-3). Callers pass their **unfiltered**
 * edges, so toggling a chip cannot move a node — while a genuine data change (a describe pass
 * adding an edge between two known nodes) still re-lays out, which keying on `ids` alone would
 * have missed.
 */
export function layoutKey(ids: string[], edges: LayoutEdge[]): string {
  return `${ids.length}:${edges.length}|${ids.join(",")}|${edges
    .map((e) => `${e.a}>${e.b}`)
    .sort()
    .join(",")}`;
}
