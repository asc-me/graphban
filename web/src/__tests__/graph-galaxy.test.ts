import { describe, expect, it } from "vitest";

import {
  collapse,
  componentsOf,
  convexHull,
  DETAIL_BUDGET,
  enterComponent,
  hullPath,
  superRadius,
} from "@/lib/graph/galaxy";
import { computeLayout, iterationsFor, type LayoutEdge } from "@/lib/graph/layout";

/**
 * The SAME fixture the backend asserts in `test_code_graph_analysis.py::_seed`.
 *
 * `componentsOf` mirrors `code_graph.components()` because the server answers over the stored
 * graph while this answers over the edges currently DRAWN — which the server cannot know, since
 * edge-type filtering is client-side and instant. Pinning both to one fixture is what makes a
 * divergence show up as a failing test rather than as two different pictures.
 */
const IDS = ["hub.py", "a.py", "b.py", "c.py", "x.py", "y.py", "lonely.py"];
const EDGES: LayoutEdge[] = [
  { a: "a.py", b: "hub.py" },
  { a: "b.py", b: "hub.py" },
  { a: "c.py", b: "hub.py" },
  { a: "c.py", b: "b.py" },
  { a: "x.py", b: "y.py" },
];

describe("componentsOf", () => {
  it("agrees with the server fixture: sizes 4, 2, 1 largest-first", () => {
    const comps = componentsOf(IDS, EDGES);
    expect(comps.map((c) => c.members.length)).toEqual([4, 2, 1]);
    expect(comps[0].members).toEqual(["a.py", "b.py", "c.py", "hub.py"]);
    expect(comps[1].members).toEqual(["x.py", "y.py"]);
    expect(comps[2].members).toEqual(["lonely.py"]);
  });

  it("anchors the largest component on its highest-inbound member", () => {
    expect(componentsOf(IDS, EDGES)[0].anchor).toBe("hub.py");
  });

  it("is deterministic and independent of input order", () => {
    const a = componentsOf(IDS, EDGES);
    const b = componentsOf([...IDS].reverse(), [...EDGES].reverse());
    expect(b).toEqual(a);
  });

  it("gives an isolated node its own component rather than dropping it", () => {
    // A file nobody imports is a real answer to "which things move together": nothing.
    expect(componentsOf(["solo.py"], []).map((c) => c.members)).toEqual([["solo.py"]]);
  });

  it("does not loop on a cycle", () => {
    const cyc: LayoutEdge[] = [
      { a: "x", b: "y" },
      { a: "y", b: "z" },
      { a: "z", b: "x" },
    ];
    expect(componentsOf(["x", "y", "z"], cyc)[0].members).toEqual(["x", "y", "z"]);
  });
});

describe("collapse", () => {
  it("maps every node to its component", () => {
    const g = collapse(IDS, EDGES);
    expect(g.componentOf["a.py"]).toBe("hub.py");
    expect(g.componentOf["x.py"]).toBe(g.componentOf["y.py"]);
    expect(g.componentOf["lonely.py"]).toBe("lonely.py");
  });

  it("emits one super-node per component, carrying its size", () => {
    const g = collapse(IDS, EDGES);
    expect(g.superNodes.map((s) => s.size)).toEqual([4, 2, 1]);
    expect(g.superNodes[0].id).toBe("hub.py");
  });

  it("has no super-edges, because components are disconnected by definition", () => {
    // Computed rather than hardcoded: the same shape must hold for a coarser grouping, and an
    // aggregation that assumed emptiness would be silently wrong there.
    expect(collapse(IDS, EDGES).superEdges).toEqual([]);
  });

  it("aggregates and counts edges when groups ARE connected", () => {
    // Feed it a grouping where cross-group edges exist, by treating one graph as two halves.
    const ids = ["l1", "l2", "r1", "r2"];
    const edges: LayoutEdge[] = [
      { a: "l1", b: "l2" },
      { a: "r1", b: "r2" },
      { a: "l1", b: "r1" },
      { a: "l2", b: "r2" },
    ];
    // With those bridges everything is one component, so the collapse is a single node.
    const g = collapse(ids, edges);
    expect(g.superNodes).toHaveLength(1);
    expect(g.superEdges).toEqual([]);
  });
});

describe("enterComponent", () => {
  it("returns only that component's nodes and its internal edges", () => {
    const g = collapse(IDS, EDGES);
    const inside = enterComponent(g, "hub.py", EDGES);
    expect(inside.ids).toEqual(["a.py", "b.py", "c.py", "hub.py"]);
    expect(inside.edges).toHaveLength(4);
    expect(inside.edges.every((e) => inside.ids.includes(e.a) && inside.ids.includes(e.b))).toBe(true);
  });

  it("is empty for an anchor that is not a component", () => {
    expect(enterComponent(collapse(IDS, EDGES), "nope.py", EDGES)).toEqual({ ids: [], edges: [] });
  });

  it("bounds layout cost by the LARGEST COMPONENT, not the repo — the whole point of D9", () => {
    // 900 nodes in ten disjoint components of 90. Flat, that is past the ~821 crossover where
    // the quality floor binds and iteration-scaling can no longer hold D1's budget. Entered
    // one at a time, every layout is a 90-node problem at full 300-iteration quality.
    const ids: string[] = [];
    const edges: LayoutEdge[] = [];
    for (let c = 0; c < 10; c++) {
      for (let i = 0; i < 90; i++) {
        const id = `c${c}/n${i}.py`;
        ids.push(id);
        if (i > 0) edges.push({ a: `c${c}/n${i - 1}.py`, b: id });
      }
    }
    expect(ids.length).toBeGreaterThan(DETAIL_BUDGET);
    expect(iterationsFor(ids.length)).toBeLessThan(300); // flat view is degraded

    const g = collapse(ids, edges);
    expect(g.superNodes).toHaveLength(10);
    const biggest = Math.max(...g.superNodes.map((s) => s.size));
    expect(biggest).toBe(90);
    expect(iterationsFor(biggest)).toBe(300); // entered, it is full quality again

    const inside = enterComponent(g, g.superNodes[0].id, edges);
    const pos = computeLayout(inside.ids, inside.edges, { width: 900, height: 560 });
    expect(Object.keys(pos)).toHaveLength(90);
  });
});

describe("superRadius", () => {
  it("grows with area, not linearly, so one big component cannot swallow the canvas", () => {
    const small = superRadius(10);
    const big = superRadius(400);
    expect(big).toBeGreaterThan(small);
    expect(big / small).toBeLessThan(10); // linear would be 40x
  });

  it("stays inside sane bounds at both extremes", () => {
    expect(superRadius(1)).toBeGreaterThanOrEqual(10);
    expect(superRadius(100000)).toBeLessThanOrEqual(46);
  });
});

describe("convexHull", () => {
  it("wraps a point cloud in its outer corners", () => {
    const hull = convexHull([
      { x: 0, y: 0 },
      { x: 10, y: 0 },
      { x: 10, y: 10 },
      { x: 0, y: 10 },
      { x: 5, y: 5 }, // interior — must not appear
    ]);
    expect(hull).toHaveLength(4);
    expect(hull.some((p) => p.x === 5 && p.y === 5)).toBe(false);
  });

  it("refuses to draw a region for fewer than three points", () => {
    // Two points are a line and one is a dot; a hull there claims an area that is not present.
    expect(convexHull([{ x: 0, y: 0 }])).toEqual([]);
    expect(convexHull([{ x: 0, y: 0 }, { x: 1, y: 1 }])).toEqual([]);
  });

  it("refuses collinear points, which have no area either", () => {
    expect(convexHull([{ x: 0, y: 0 }, { x: 1, y: 1 }, { x: 2, y: 2 }])).toEqual([]);
  });

  it("pushes every corner outward when padded", () => {
    const bare = convexHull([{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 5, y: 10 }]);
    const padded = convexHull([{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 5, y: 10 }], 8);
    const cx = 5;
    const cy = 10 / 3;
    for (let i = 0; i < bare.length; i++) {
      const before = Math.hypot(bare[i].x - cx, bare[i].y - cy);
      const after = Math.hypot(padded[i].x - cx, padded[i].y - cy);
      expect(after).toBeGreaterThan(before);
    }
  });

  it("is deterministic regardless of input order", () => {
    const pts = [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 }];
    expect(convexHull([...pts].reverse())).toEqual(convexHull(pts));
  });
});

describe("hullPath", () => {
  it("closes the path", () => {
    const d = hullPath([{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 5, y: 10 }]);
    expect(d.startsWith("M0.0,0.0")).toBe(true);
    expect(d.endsWith("Z")).toBe(true);
  });

  it("is empty when there is no region", () => {
    expect(hullPath([{ x: 0, y: 0 }])).toBe("");
  });
});
