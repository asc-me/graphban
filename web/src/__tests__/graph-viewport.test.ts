import { describe, expect, it } from "vitest";

import { computeLayout, type LayoutEdge } from "@/lib/graph/layout";
import { degrees, topByDegree } from "@/lib/graph/metrics";
import { matchIds } from "@/lib/graph/useGraphFind";
import {
  clampZoom,
  fitViewport,
  IDENTITY,
  MAX_ZOOM,
  MIN_ZOOM,
  zoomAbout,
} from "@/lib/graph/useGraphViewport";

const W = 900;
const H = 560;

/** Apply a viewport the way the `<g transform>` does: translate, then scale. */
const project = (v: { k: number; x: number; y: number }, p: { x: number; y: number }) => ({
  x: v.x + p.x * v.k,
  y: v.y + p.y * v.k,
});

describe("zoomAbout", () => {
  it("keeps the point under the cursor exactly where it was", () => {
    // This is the whole feature: zooming about the centre instead makes the view walk away
    // from whatever the user is pointing at.
    const cursor = { x: 700, y: 120 };
    const before = IDENTITY;
    const world = { x: (cursor.x - before.x) / before.k, y: (cursor.y - before.y) / before.k };
    const after = zoomAbout(before, 1.6, cursor.x, cursor.y);
    const moved = project(after, world);
    expect(moved.x).toBeCloseTo(cursor.x, 9);
    expect(moved.y).toBeCloseTo(cursor.y, 9);
  });

  it("holds the anchor across a chain of zooms, in and out", () => {
    const cursor = { x: 310, y: 400 };
    let v = IDENTITY;
    const world = { x: cursor.x, y: cursor.y };
    for (const f of [1.3, 1.3, 0.7, 2.1, 0.5]) {
      v = zoomAbout(v, f, cursor.x, cursor.y);
      const moved = project(v, world);
      expect(moved.x).toBeCloseTo(cursor.x, 8);
      expect(moved.y).toBeCloseTo(cursor.y, 8);
    }
  });

  it("clamps to the zoom range, and a clamped zoom returns the SAME OBJECT", () => {
    // Reference identity, not deep equality. The translation is algebraically unchanged when
    // k is unchanged, so `toEqual` here passes whether or not the early return exists — it
    // asserts a tautology. Returning the same reference is the real, breakable property: it
    // is what lets React bail out instead of re-rendering the whole scene on every wheel tick
    // once the user has bottomed out the zoom.
    let v = IDENTITY;
    for (let i = 0; i < 40; i++) v = zoomAbout(v, 2, 450, 280);
    expect(v.k).toBe(MAX_ZOOM);
    expect(zoomAbout(v, 2, 450, 280)).toBe(v);

    let out = IDENTITY;
    for (let i = 0; i < 40; i++) out = zoomAbout(out, 0.5, 450, 280);
    expect(out.k).toBe(MIN_ZOOM);
    expect(zoomAbout(out, 0.5, 450, 280)).toBe(out);
  });
});

describe("clampZoom", () => {
  it("bounds both ends and passes the middle through", () => {
    expect(clampZoom(0.01)).toBe(MIN_ZOOM);
    expect(clampZoom(1000)).toBe(MAX_ZOOM);
    expect(clampZoom(2)).toBe(2);
  });
});

describe("fitViewport", () => {
  it("centres the fitted content in the frame", () => {
    const pts = [
      { x: 100, y: 100 },
      { x: 300, y: 260 },
    ];
    const v = fitViewport(pts, W, H);
    const mid = project(v, { x: 200, y: 180 });
    expect(mid.x).toBeCloseTo(W / 2, 6);
    expect(mid.y).toBeCloseTo(H / 2, 6);
  });

  it("brings every point inside the frame", () => {
    const pts = [
      { x: -400, y: -200 },
      { x: 1800, y: 1200 },
      { x: 50, y: 900 },
    ];
    const v = fitViewport(pts, W, H);
    for (const p of pts) {
      const q = project(v, p);
      expect(q.x).toBeGreaterThanOrEqual(-1);
      expect(q.x).toBeLessThanOrEqual(W + 1);
      expect(q.y).toBeGreaterThanOrEqual(-1);
      expect(q.y).toBeLessThanOrEqual(H + 1);
    }
  });

  it("does not blow up on a single point or an empty set", () => {
    expect(fitViewport([], W, H)).toEqual(IDENTITY);
    const one = fitViewport([{ x: 10, y: 10 }], W, H);
    expect(Number.isFinite(one.k)).toBe(true);
    expect(one.k).toBeLessThanOrEqual(MAX_ZOOM);
    expect(one.k).toBeGreaterThanOrEqual(MIN_ZOOM);
  });
});

describe("pinning", () => {
  const IDS = ["a.py", "b.py", "c.py", "d.py", "e.py"];
  const EDGES: LayoutEdge[] = [
    { a: "a.py", b: "b.py" },
    { a: "b.py", b: "c.py" },
    { a: "c.py", b: "d.py" },
    { a: "d.py", b: "e.py" },
  ];

  it("holds a pinned node at exactly the position given", () => {
    const held = { "c.py": { x: 42, y: 17 } };
    const pos = computeLayout(IDS, EDGES, { width: W, height: H, pinned: held });
    expect(pos["c.py"]).toEqual({ x: 42, y: 17 });
  });

  it("still lets a pinned node REPEL its neighbours", () => {
    // Isolated deliberately: the pinned node has NO edges, so the only force it can transmit
    // is repulsion. Comparing a pinned-vs-unpinned run on a connected node proves nothing —
    // the springs alone move the neighbours, so that version passed even with repulsion from
    // pinned nodes removed entirely. Here, moving WHERE the pin sits must move the free nodes;
    // if pinned nodes exerted no force the two runs would be identical.
    const ids = ["free1", "free2", "island"];
    const edges: LayoutEdge[] = [{ a: "free1", b: "free2" }];
    const near = computeLayout(ids, edges, {
      width: W,
      height: H,
      pinned: { island: { x: 450, y: 280 } },
    });
    const far = computeLayout(ids, edges, {
      width: W,
      height: H,
      pinned: { island: { x: 20, y: 20 } },
    });
    const shifted = ["free1", "free2"].some(
      (id) => Math.hypot(near[id].x - far[id].x, near[id].y - far[id].y) > 1,
    );
    expect(shifted).toBe(true);
  });

  it("is unchanged from the unpinned layout when nothing is pinned", () => {
    const a = computeLayout(IDS, EDGES, { width: W, height: H });
    const b = computeLayout(IDS, EDGES, { width: W, height: H, pinned: {} });
    expect(a).toEqual(b);
  });

  it("pins every node without producing NaN", () => {
    const all = Object.fromEntries(IDS.map((id, i) => [id, { x: i * 10, y: i * 5 }]));
    const pos = computeLayout(IDS, EDGES, { width: W, height: H, pinned: all });
    expect(pos).toEqual(all);
  });
});

describe("matchIds", () => {
  const IDS = [
    "backend/app/services/items.py",
    "backend/app/services/items.py::claim_next",
    "web/src/lib/queries.ts",
  ];

  it("is empty for a blank query, so 'no search' is distinguishable from 'no hits'", () => {
    expect(matchIds(IDS, "", (id) => id).size).toBe(0);
    expect(matchIds(IDS, "   ", (id) => id).size).toBe(0);
  });

  it("matches case-insensitively on any part of the text", () => {
    expect(matchIds(IDS, "CLAIM_NEXT", (id) => id)).toEqual(
      new Set(["backend/app/services/items.py::claim_next"]),
    );
    expect(matchIds(IDS, "services", (id) => id).size).toBe(2);
  });

  it("returns an empty set for a query that hits nothing", () => {
    expect(matchIds(IDS, "zzz-nope", (id) => id).size).toBe(0);
  });

  it("searches whatever text the view supplies, not only the id", () => {
    const named: Record<string, string> = { "web/src/lib/queries.ts": "useCodeMap" };
    const hits = matchIds(IDS, "usecodemap", (id) => `${id} ${named[id] ?? ""}`);
    expect(hits).toEqual(new Set(["web/src/lib/queries.ts"]));
  });
});

describe("topByDegree", () => {
  const IDS = ["hub", "a", "b", "c", "lonely"];
  const EDGES: LayoutEdge[] = [
    { a: "hub", b: "a" },
    { a: "hub", b: "b" },
    { a: "hub", b: "c" },
    { a: "a", b: "b" },
  ];

  it("counts undirected degree and includes isolated nodes as zero", () => {
    expect(degrees(IDS, EDGES)).toEqual({ hub: 3, a: 2, b: 2, c: 1, lonely: 0 });
  });

  it("ranks the hub first", () => {
    expect([...topByDegree(IDS, EDGES, 1)]).toEqual(["hub"]);
  });

  it("breaks ties by id so the label set does not flicker between renders", () => {
    const once = [...topByDegree(IDS, EDGES, 3)];
    const again = [...topByDegree([...IDS].reverse(), [...EDGES].reverse(), 3)];
    expect(again).toEqual(once);
  });

  it("handles n larger than the graph, and n of zero", () => {
    expect(topByDegree(IDS, EDGES, 99).size).toBe(IDS.length);
    expect(topByDegree(IDS, EDGES, 0).size).toBe(0);
  });

  it("ignores edges naming nodes that are not in the id set", () => {
    expect(degrees(["a"], [{ a: "a", b: "ghost" }])).toEqual({ a: 1 });
  });
});
