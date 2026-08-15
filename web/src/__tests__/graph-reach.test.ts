import { describe, expect, it } from "vitest";

import type { LayoutEdge } from "@/lib/graph/layout";
import { adjacency, withinHops } from "@/lib/graph/metrics";

//   a — b — c — d        (a chain)
//   b — e                (a spur off b)
//   island               (unconnected)
const IDS = ["a", "b", "c", "d", "e", "island"];
const EDGES: LayoutEdge[] = [
  { a: "a", b: "b" },
  { a: "b", b: "c" },
  { a: "c", b: "d" },
  { a: "b", b: "e" },
];

describe("adjacency", () => {
  it("is undirected and lists every id, isolated ones included", () => {
    const adj = adjacency(IDS, EDGES);
    expect(adj.a).toEqual(["b"]);
    expect(adj.b.sort()).toEqual(["a", "c", "e"]);
    expect(adj.island).toEqual([]);
  });

  it("drops an edge naming a node outside the id set", () => {
    // Half-adding it would make degree and reach disagree about the same graph.
    const adj = adjacency(["a"], [{ a: "a", b: "ghost" }]);
    expect(adj.a).toEqual([]);
    expect("ghost" in adj).toBe(false);
  });
});

describe("withinHops", () => {
  it("includes the origin at depth 0 and nothing else", () => {
    expect(withinHops(IDS, EDGES, "b", 0)).toEqual(new Set(["b"]));
  });

  it("is the direct neighbourhood at depth 1", () => {
    expect(withinHops(IDS, EDGES, "b", 1)).toEqual(new Set(["b", "a", "c", "e"]));
  });

  it("adds exactly one ring per extra hop — the thing one-hop code could not do", () => {
    expect(withinHops(IDS, EDGES, "a", 1)).toEqual(new Set(["a", "b"]));
    expect(withinHops(IDS, EDGES, "a", 2)).toEqual(new Set(["a", "b", "c", "e"]));
    expect(withinHops(IDS, EDGES, "a", 3)).toEqual(new Set(["a", "b", "c", "d", "e"]));
  });

  it("saturates at the component rather than growing forever", () => {
    const far = withinHops(IDS, EDGES, "a", 99);
    expect(far).toEqual(new Set(["a", "b", "c", "d", "e"]));
    expect(far.has("island")).toBe(false);
  });

  it("never revisits a node, so a cycle cannot loop it", () => {
    const cyc: LayoutEdge[] = [
      { a: "x", b: "y" },
      { a: "y", b: "z" },
      { a: "z", b: "x" },
    ];
    expect(withinHops(["x", "y", "z"], cyc, "x", 10)).toEqual(new Set(["x", "y", "z"]));
  });

  it("returns an isolated node alone at any depth", () => {
    expect(withinHops(IDS, EDGES, "island", 5)).toEqual(new Set(["island"]));
  });

  it("returns empty for an origin that is not in the graph", () => {
    // Empty, NOT a set containing the unknown id — a selection that has been filtered away
    // must light nothing rather than inventing a node.
    expect(withinHops(IDS, EDGES, "nope", 2)).toEqual(new Set());
  });

  it("treats a negative depth as zero rather than throwing", () => {
    expect(withinHops(IDS, EDGES, "b", -3)).toEqual(new Set(["b"]));
  });

  it("is symmetric — reach does not depend on edge direction", () => {
    const flipped = EDGES.map((e) => ({ a: e.b, b: e.a }));
    expect(withinHops(IDS, flipped, "a", 2)).toEqual(withinHops(IDS, EDGES, "a", 2));
  });
});
