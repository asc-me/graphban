import { describe, expect, it } from "vitest";

import { pathHighlight } from "@/lib/graph/pathHighlight";
import type { CodePath } from "@/lib/types";

/**
 * PRD-20 AC-19, the "off-path dimmed" clause (GRPH-481).
 *
 * The renderer already dims anything outside a `{hlNodes, hlEdges}` set — that is how the reach
 * ring works, and it was verified against the live graph during the walk (88 lit / 66 dim, zero
 * violations either way). Returning the same shape means a path REPLACES the reach dimming
 * instead of introducing a second set of rules over the same pixels, so these tests are about
 * what belongs in the set and nothing else.
 */
const path = (hops: CodePath["hops"], over: Partial<CodePath> = {}): CodePath => ({
  a: hops[0]?.src ?? "",
  b: hops[hops.length - 1]?.dst ?? "",
  found: true,
  missing: [],
  hops,
  ...over,
});

const EDGES = [
  { src: "a.py", dst: "b.py", type: "imports" },      // 0 — on the walk
  { src: "c.py", dst: "b.py", type: "references" },   // 1 — on the walk, drawn BACKWARDS
  { src: "a.py", dst: "b.py", type: "references" },   // 2 — same pair, WRONG type
  { src: "a.py", dst: "z.py", type: "imports" },      // 3 — off the path entirely
  { src: "b.py", dst: "c.py", type: "calls" },        // 4 — same pair as hop 2, wrong type
];

const WALK = path([
  { src: "a.py", dst: "b.py", type: "imports", forward: true },
  { src: "b.py", dst: "c.py", type: "references", forward: false },
]);

describe("only the hops light", () => {
  it("lights the edge that was walked", () => {
    expect(pathHighlight(WALK, EDGES)!.hlEdges.has("0")).toBe(true);
  });

  it("lights a hop whose drawn edge runs the other way", () => {
    // The walk is undirected: hop 2 is b.py -> c.py, and the edge is drawn c.py -> b.py.
    // `forward: false` is the record of exactly that, and must not cause a miss.
    expect(pathHighlight(WALK, EDGES)!.hlEdges.has("1")).toBe(true);
  });

  it("does NOT light a different edge between the same two nodes", () => {
    const hl = pathHighlight(WALK, EDGES)!;
    // Two nodes can be joined by both `imports` and `references`; only one was the hop.
    // Matching on endpoints alone would light a relation the route never used.
    expect(hl.hlEdges.has("2")).toBe(false);
    expect(hl.hlEdges.has("4")).toBe(false);
  });

  it("does not light an edge off the path", () => {
    expect(pathHighlight(WALK, EDGES)!.hlEdges.has("3")).toBe(false);
  });

  it("lights exactly as many edges as there were hops", () => {
    expect(pathHighlight(WALK, EDGES)!.hlEdges.size).toBe(WALK.hops.length);
  });

  it("carries every node on the route and no others", () => {
    expect([...pathHighlight(WALK, EDGES)!.hlNodes].sort()).toEqual(["a.py", "b.py", "c.py"]);
  });
});

describe("no path means no highlight, so nothing dims for nothing", () => {
  it("returns null when there is no path at all", () => {
    expect(pathHighlight(null, EDGES)).toBeNull();
  });

  it("returns null when the endpoints are unreachable", () => {
    expect(pathHighlight(path([], { found: false }), EDGES)).toBeNull();
  });

  it("returns null for a found-but-empty walk rather than dimming the whole graph", () => {
    // A node to itself: `found`, zero hops. Returning an empty highlight would dim every
    // edge on the canvas and light none — a blank graph presented as an answer.
    expect(pathHighlight(path([], { found: true }), EDGES)).toBeNull();
  });
});
