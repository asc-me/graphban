import { describe, expect, it } from "vitest";

import {
  BASE_ITERATIONS,
  computeLayout,
  iterationsFor,
  layoutKey,
  type LayoutEdge,
} from "@/lib/graph/layout";

const W = 900;
const H = 560;

/**
 * The layout exactly as it existed in `CodeGraphView.tsx:44` and `LinksGraphView.tsx:25`
 * before PRD-20 D1 extracted it — kept verbatim so the extraction can be proved to move no
 * node. If `computeLayout` is ever legitimately retuned, this fixture is what says so out
 * loud rather than letting a refactor hide a visual change.
 */
function legacyLayout(ids: string[], edges: LayoutEdge[]): Record<string, { x: number; y: number }> {
  const n = ids.length;
  const cx = W / 2;
  const cy = H / 2;
  const r = Math.min(W, H) / 2.6;
  const pos: Record<string, { x: number; y: number }> = {};
  ids.forEach((id, i) => {
    const a = (2 * Math.PI * i) / Math.max(1, n);
    pos[id] = { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) };
  });

  const REST = 150;
  for (let iter = 0; iter < 300; iter++) {
    const disp: Record<string, { x: number; y: number }> = {};
    ids.forEach((id) => (disp[id] = { x: 0, y: 0 }));
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const u = ids[i];
        const v = ids[j];
        let dx = pos[u].x - pos[v].x;
        let dy = pos[u].y - pos[v].y;
        const d2 = dx * dx + dy * dy || 0.01;
        const f = 26000 / d2;
        const d = Math.sqrt(d2);
        dx /= d;
        dy /= d;
        disp[u].x += dx * f;
        disp[u].y += dy * f;
        disp[v].x -= dx * f;
        disp[v].y -= dy * f;
      }
    }
    for (const e of edges) {
      if (!pos[e.a] || !pos[e.b]) continue;
      let dx = pos[e.b].x - pos[e.a].x;
      let dy = pos[e.b].y - pos[e.a].y;
      const d = Math.hypot(dx, dy) || 0.01;
      const f = (d - REST) * 0.06;
      dx = (dx / d) * f;
      dy = (dy / d) * f;
      disp[e.a].x += dx;
      disp[e.a].y += dy;
      disp[e.b].x -= dx;
      disp[e.b].y -= dy;
    }
    for (const id of ids) {
      disp[id].x += (cx - pos[id].x) * 0.02;
      disp[id].y += (cy - pos[id].y) * 0.02;
      const step = 0.6;
      pos[id].x += Math.max(-14, Math.min(14, disp[id].x * step));
      pos[id].y += Math.max(-14, Math.min(14, disp[id].y * step));
    }
  }
  return pos;
}

const IDS = [
  "backend/app/services/items.py",
  "backend/app/services/fleet.py",
  "backend/app/services/clustering.py",
  "backend/app/services/code_graph.py",
  "web/src/lib/queries.ts",
  "web/src/features/code/CodeGraphView.tsx",
  "web/src/features/links/LinksGraphView.tsx",
];

const EDGES: LayoutEdge[] = [
  { a: "backend/app/services/fleet.py", b: "backend/app/services/items.py" },
  { a: "backend/app/services/code_graph.py", b: "backend/app/services/clustering.py" },
  { a: "web/src/features/code/CodeGraphView.tsx", b: "web/src/lib/queries.ts" },
  { a: "web/src/features/links/LinksGraphView.tsx", b: "web/src/lib/queries.ts" },
];

describe("computeLayout", () => {
  it("moves no node relative to the two copies it replaced", () => {
    const now = computeLayout(IDS, EDGES, { width: W, height: H });
    const before = legacyLayout(IDS, EDGES);
    for (const id of IDS) {
      expect(now[id].x).toBeCloseTo(before[id].x, 10);
      expect(now[id].y).toBeCloseTo(before[id].y, 10);
    }
  });

  it("is deterministic — identical inputs give byte-identical output", () => {
    const a = computeLayout(IDS, EDGES, { width: W, height: H });
    const b = computeLayout(IDS, EDGES, { width: W, height: H });
    expect(a).toEqual(b);
  });

  it("does not depend on edge order", () => {
    const a = computeLayout(IDS, EDGES, { width: W, height: H });
    const b = computeLayout(IDS, [...EDGES].reverse(), { width: W, height: H });
    for (const id of IDS) {
      expect(a[id].x).toBeCloseTo(b[id].x, 6);
      expect(a[id].y).toBeCloseTo(b[id].y, 6);
    }
  });

  it("places every id and nothing else", () => {
    const pos = computeLayout(IDS, EDGES, { width: W, height: H });
    expect(Object.keys(pos).sort()).toEqual([...IDS].sort());
  });

  it("ignores an edge naming an unknown node rather than throwing", () => {
    const pos = computeLayout(IDS, [{ a: "nope.py", b: "also-nope.py" }], { width: W, height: H });
    expect(Object.keys(pos)).toHaveLength(IDS.length);
  });

  it("handles the empty and single-node cases", () => {
    expect(computeLayout([], [], { width: W, height: H })).toEqual({});
    const one = computeLayout(["solo.py"], [], { width: W, height: H });
    expect(Number.isFinite(one["solo.py"].x)).toBe(true);
    expect(Number.isFinite(one["solo.py"].y)).toBe(true);
  });
});

describe("iterationsFor", () => {
  it("is unchanged at every size we actually render today", () => {
    // The live code graph is ~123 nodes; full describe coverage lands near 400.
    expect(iterationsFor(1)).toBe(BASE_ITERATIONS);
    expect(iterationsFor(123)).toBe(BASE_ITERATIONS);
    expect(iterationsFor(300)).toBe(BASE_ITERATIONS);
  });

  it("holds total pair-work flat from the knee up to the 800-node supported ceiling", () => {
    const work = (n: number) => iterationsFor(n) * n * n;
    const atKnee = work(300);
    for (const n of [400, 500, 640, 800]) {
      expect(work(n)).toBeLessThanOrEqual(atKnee * 1.1);
    }
  });

  it("stops being able to hold the budget above ~800 — which is why D9 exists", () => {
    // The 40-pass quality floor and the flat-work budget are in direct conflict, and the floor
    // wins: below ~821 nodes the budget sets the count, above it the floor does and total work
    // grows as O(n²) again. That crossover is not a tuning accident to be papered over — it is
    // the real ceiling of an O(n²) layout, and it is where PRD-20 D1's 800-node budget comes
    // from. Past it the answer is D9's galaxy view (or Barnes-Hut), never more tuning here.
    const work = (n: number) => iterationsFor(n) * n * n;
    const atKnee = work(300);
    expect(work(900)).toBeGreaterThan(atKnee * 1.1);
    expect(iterationsFor(900)).toBe(40);
  });

  it("never degenerates to zero passes", () => {
    expect(iterationsFor(100000)).toBeGreaterThanOrEqual(40);
  });
});

describe("layoutKey", () => {
  it("IS sensitive to the edge set — which is why the views must not pass a filtered one", () => {
    // The previous version of this test compared `layoutKey(IDS, EDGES)` with
    // `layoutKey(IDS, [...EDGES])` and called it AC-3. A copy of one array cannot differ
    // from itself, so it passed under every implementation including a broken one.
    //
    // The real property is the opposite of what it asserted: the key MUST change when the
    // edges change, because the hook memoizes on ids AND edges. Keying on ids alone would
    // miss a describe-pass edge, so that is the right call — and it means AC-3 cannot be
    // satisfied here at all. It is a CALL-SITE guarantee, pinned below.
    // `LayoutEdge` is deliberately just {a, b} — the layout has no concept of edge type,
    // which is itself why the filtering has to happen above it. A chip toggle reaches this
    // layer only as a SUBSET, so that is what is simulated.
    const afterAChipToggle = EDGES.slice(0, -1);
    expect(afterAChipToggle.length).toBeLessThan(EDGES.length);
    expect(layoutKey(IDS, afterAChipToggle)).not.toBe(layoutKey(IDS, EDGES));
  });

  it("changes when the node set changes", () => {
    expect(layoutKey([...IDS, "new.py"], EDGES)).not.toBe(layoutKey(IDS, EDGES));
  });

  it("changes when a real edge appears between two known nodes", () => {
    // Keying on ids alone would miss this and leave the map stale after a describe pass.
    const extra = [...EDGES, { a: IDS[0], b: IDS[4] }];
    expect(layoutKey(IDS, extra)).not.toBe(layoutKey(IDS, EDGES));
  });

  it("is stable under edge reordering", () => {
    expect(layoutKey(IDS, [...EDGES].reverse())).toBe(layoutKey(IDS, EDGES));
  });
});
