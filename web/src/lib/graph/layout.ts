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
  /**
   * Nodes the user has dragged, held at exactly these positions (PRD-20 D2).
   *
   * A pinned node still *exerts* repulsion and spring force on everything else — it is part of
   * the graph, not removed from it — but it is skipped in the integrate step, so it does not
   * move. Dragging is how a human says "this one matters, stop moving it", and a pin that
   * drifted back would make the gesture a lie.
   */
  pinned?: Record<string, Pos>;
  /**
   * Area membership for the clustered layout (GRPH-1051 / Build.dc.html). When set, the
   * code graph places a module at each area's centre and its children on rings rather than
   * running the force layout. The links graph and the galaxy view omit this, so they keep
   * the extraction-identical force path.
   */
  membership?: Record<string, string>;
  /** Kind per node, used only with `membership` to pick a ring. */
  kinds?: Record<string, string>;
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

/**
 * Dispatch used by the worker and the jsdom fallback. Force layout stays the default so the
 * extraction test (and the links graph) keep their positions; clustered is opt-in.
 */
export function runLayout(
  ids: string[],
  edges: LayoutEdge[],
  opts: LayoutOpts,
): Record<string, Pos> {
  if (opts.membership && Object.keys(opts.membership).length > 0) {
    return computeClusteredLayout(ids, opts);
  }
  return computeLayout(ids, edges, opts);
}

/**
 * Build.dc.html's ring layout: one hub per area, files / symbols / docs on concentric rings.
 *
 * Geometric, not force-directed — that is the point. Force layout does not group directories,
 * so dashed hulls on top of it are overlapping blobs. Rings are deterministic (sorted ids,
 * sorted areas, no `Math.random`) and honour pins the same way the force path does: a pinned
 * node is placed at the held coordinate after everyone else is laid out.
 *
 * Radii are scaled from the design's 1200×760 viewBox down to this 900×560 one (~0.5, because
 * a full-size ring of 168 would collide two clusters on the smaller canvas).
 */
const RING_FOR: Record<string, number> = {
  file: 54,
  symbol: 84,
  doc: 35,
  config: 35,
};
const RING_DEFAULT = 50;
const HUB_PAD = 22;

export function computeClusteredLayout(
  ids: string[],
  opts: LayoutOpts,
): Record<string, Pos> {
  const { width, height, pinned, membership = {}, kinds = {} } = opts;
  const cx = width / 2;
  const cy = height / 2;
  const pos: Record<string, Pos> = {};
  if (ids.length === 0) return pos;

  const buckets = new Map<string, string[]>();
  for (const id of ids) {
    const area = membership[id] ?? id;
    const list = buckets.get(area) ?? [];
    list.push(id);
    buckets.set(area, list);
  }
  const areas = [...buckets.keys()].sort();

  const radii = areas.map((area) => {
    const members = buckets.get(area)!;
    let maxR = HUB_PAD;
    for (const id of members) {
      const kind = kinds[id] ?? "";
      if (kind === "module") continue;
      maxR = Math.max(maxR, RING_FOR[kind] ?? RING_DEFAULT);
    }
    return maxR + HUB_PAD;
  });

  const centres: Pos[] = [];
  if (areas.length === 1) {
    centres.push({ x: cx, y: cy });
  } else {
    let R = Math.min(width, height) / 2.8;
    const gap = 24;
    let need = 0;
    for (let i = 0; i < areas.length; i++) {
      const pair = radii[i] + radii[(i + 1) % areas.length] + gap;
      need = Math.max(need, pair / (2 * Math.sin(Math.PI / areas.length)));
    }
    R = Math.max(R, need);
    areas.forEach((_, i) => {
      const angle = (2 * Math.PI * i) / areas.length - Math.PI / 2;
      centres.push({ x: cx + R * Math.cos(angle), y: cy + R * Math.sin(angle) });
    });
  }

  areas.forEach((area, ai) => {
    const origin = centres[ai];
    const members = [...(buckets.get(area) ?? [])].sort();
    const hub = members.find((id) => kinds[id] === "module");
    if (hub) pos[hub] = { x: origin.x, y: origin.y };
    const rings = new Map<string, string[]>();
    for (const id of members) {
      if (id === hub) continue;
      const kind = kinds[id] ?? "file";
      const list = rings.get(kind) ?? [];
      list.push(id);
      rings.set(kind, list);
    }
    for (const [kind, ringIds] of [...rings.entries()].sort(([a], [b]) => (a < b ? -1 : 1))) {
      const rr = RING_FOR[kind] ?? RING_DEFAULT;
      const n = ringIds.length;
      ringIds.forEach((id, i) => {
        const angle = (2 * Math.PI * i) / Math.max(1, n) - Math.PI / 2;
        pos[id] = { x: origin.x + rr * Math.cos(angle), y: origin.y + rr * Math.sin(angle) };
      });
    }
  });

  if (pinned) {
    for (const id of ids) {
      const held = pinned[id];
      if (held) pos[id] = { x: held.x, y: held.y };
    }
  }
  return pos;
}

export function computeLayout(
  ids: string[],
  edges: LayoutEdge[],
  opts: LayoutOpts,
): Record<string, Pos> {
  const { width, height, pinned } = opts;
  const iterations = opts.iterations ?? iterationsFor(ids.length);
  const n = ids.length;
  const cx = width / 2;
  const cy = height / 2;
  const r = Math.min(width, height) / 2.6;

  const pos: Record<string, Pos> = {};
  ids.forEach((id, i) => {
    const held = pinned?.[id];
    if (held) {
      pos[id] = { x: held.x, y: held.y };
      return;
    }
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

    // Center pull + integrate (capped step). A pinned node is skipped here and nowhere else:
    // it has already contributed its forces above, it just does not accept any itself.
    for (const id of ids) {
      if (pinned?.[id]) continue;
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
export function layoutKey(ids: string[], edges: LayoutEdge[], cluster = ""): string {
  const base = `${ids.length}:${edges.length}|${ids.join(",")}|${edges
    .map((e) => `${e.a}>${e.b}`)
    .sort()
    .join(",")}`;
  return cluster ? `${base}|${cluster}` : base;
}
