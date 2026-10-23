import { describe, expect, it } from "vitest";

import { componentsOf, convexHull, hullPath } from "@/lib/graph/galaxy";
import type { LayoutEdge } from "@/lib/graph/layout";

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
