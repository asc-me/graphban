import { describe, expect, it } from "vitest";

import { areaColor, areasAtScale, type ScaleEdge, type ScaleNode } from "@/lib/graph/areas";
import { dense, denseArea, sparse } from "@/lib/graph/__fixtures__/bigMap";
import { buildAreaGraph, deriveLayers } from "@/lib/graph/layers";

function mod(path: string, n: number): { nodes: ScaleNode[]; owns: ScaleEdge[] } {
  const nodes: ScaleNode[] = [{ id: path, path, kind: "module" }];
  const owns: ScaleEdge[] = [];
  for (let i = 0; i < n - 1; i++) {
    const id = `${path}/f${i}`;
    nodes.push({ id, path: id, kind: "file" });
    owns.push({ src: path, dst: id, type: "owns" });
  }
  return { nodes, owns };
}

function assemble(parts: { nodes: ScaleNode[]; owns: ScaleEdge[] }[]) {
  return {
    nodes: parts.flatMap((p) => p.nodes),
    owns: parts.flatMap((p) => p.owns),
  };
}

/** 18 depth-2 groups × 2 modules. Fresh pick is depth 2 (18 areas); depth 3 has 36. */
function hysteresisBase() {
  const parts = [];
  for (let p = 0; p < 18; p++) {
    for (const side of ["a", "b"]) {
      parts.push(mod(`s/${String(p).padStart(2, "0")}/${side}`, 10));
    }
  }
  return assemble(parts);
}

function hashRng(seed: number): () => number {
  let s = seed;
  return () => {
    s = (s + 0x6d2b79f5) | 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function shuffle<T>(items: T[], seed: number): T[] {
  const a = [...items];
  const rnd = hashRng(seed);
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(rnd() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

describe("areasAtScale", () => {
  it("returns 12–30 areas on both fixtures, and other always reports its count", () => {
    for (const map of [sparse, dense]) {
      const got = areasAtScale(map.nodes, map.owns);
      expect(got.flat).toBe(false);
      expect(got.areas.length).toBeGreaterThanOrEqual(12);
      expect(got.areas.length).toBeLessThanOrEqual(30);
      expect(got.other.count).toBe(got.other.members.length);
      expect(got.areas.reduce((s, a) => s + a.count, 0)).toBe(map.nodes.length);
      if (got.other.count > 0) {
        const labelled = got.areas[got.areas.length - 1];
        expect(labelled.id).toBe("other");
        expect(labelled.count).toBe(got.other.count);
      }
      for (const area of got.areas) expect(area.color).toBe(areaColor(area.id));
    }
  });

  it("keeps a depth inside 10–36 when nodes are added or removed, and re-picks outside it", () => {
    const base = hysteresisBase();
    expect(areasAtScale(base.nodes, base.owns).depth).toBe(2);

    const kept = areasAtScale(base.nodes, base.owns, 3);
    expect(kept.depth).toBe(3);
    expect(kept.kept).toBe(true);
    expect(kept.areas).toHaveLength(36);

    const modules = base.nodes.filter((n) => n.kind === "module").slice(0, 20);
    const addedNodes = [
      ...base.nodes,
      ...modules.map((n, i) => ({ id: `${n.path}/extra${i}`, path: `${n.path}/extra${i}`, kind: "file" })),
    ];
    const addedOwns = [
      ...base.owns,
      ...modules.map((n, i) => ({ src: n.id, dst: `${n.path}/extra${i}`, type: "owns" })),
    ];
    const added = areasAtScale(addedNodes, addedOwns, 3);
    expect(added.depth).toBe(3);
    expect(added.kept).toBe(true);

    const drop = new Set(modules.map((n) => `${n.path}/f0`));
    const removedNodes = base.nodes.filter((n) => !drop.has(n.id));
    const removedOwns = base.owns.filter((e) => !drop.has(e.dst));
    const removed = areasAtScale(removedNodes, removedOwns, 3);
    expect(removedNodes).toHaveLength(base.nodes.length - 20);
    expect(removed.depth).toBe(3);
    expect(removed.kept).toBe(true);

    const born = mod("s/18/a", 10);
    const left = areasAtScale([...base.nodes, ...born.nodes], [...base.owns, ...born.owns], 3);
    expect(left.kept).toBe(false);
    expect(left.depth).toBe(2);
  });

  it("drops a depth whose other is over 15% when a cleaner candidate exists", () => {
    const parts = [];
    for (let g = 0; g < 4; g++) {
      for (let k = 0; k < 4; k++) parts.push(mod(`g${g}/k${k}`, 8));
      for (let z = 0; z < 7; z++) parts.push(mod(`g${g}/z${z}`, 1));
    }
    const graph = assemble(parts);
    const got = areasAtScale(graph.nodes, graph.owns);
    expect(got.flat).toBe(false);
    expect(got.depth).toBe(1);
    expect(got.areas.length).toBeGreaterThanOrEqual(2);
    expect(got.other.count / graph.nodes.length).toBeLessThanOrEqual(0.15);
  });

  it("never picks fewer than 2 areas, and a flat repo is one flagged area", () => {
    const two = assemble([mod("a/one", 10), mod("b/two", 10)]);
    const split = areasAtScale(two.nodes, two.owns);
    expect(split.flat).toBe(false);
    expect(split.areas.length).toBeGreaterThanOrEqual(2);

    const flatNodes: ScaleNode[] = [0, 1, 2].map((i) => ({
      id: `only/f${i}`,
      path: `only/f${i}`,
      kind: "file",
    }));
    const flat = areasAtScale(flatNodes, []);
    expect(flat.flat).toBe(true);
    expect(flat.depth).toBeNull();
    expect(flat.areas).toHaveLength(1);
    expect(flat.areas[0].flat).toBe(true);
    expect(flat.areas[0].caption).toBe("flat repo: one area");
    expect(flat.areas[0].members).toHaveLength(3);
    expect(flat.other.count).toBe(0);
  });

  it("keeps prevDepth only when two depths are the same distance from 12–30", () => {
    const counts = [8, 8, 7, 7, 7];
    const parts = [];
    for (let g = 0; g < counts.length; g++) {
      for (let m = 0; m < counts[g]; m++) {
        parts.push(mod(`g${g}/m${String(m).padStart(2, "0")}`, 10));
      }
    }
    const graph = assemble(parts);
    expect(areasAtScale(graph.nodes, graph.owns).depth).toBe(1);
    const tied = areasAtScale(graph.nodes, graph.owns, 2);
    expect(tied.kept).toBe(false);
    expect(tied.depth).toBe(2);
  });
});

describe("deriveLayers", () => {
  it("puts a data-only area at layer 0 and a dependent one above it", () => {
    const got = deriveLayers({
      areas: ["app", "data", "mid", "top"],
      edges: [
        { src: "app", dst: "data", type: "imports" },
        { src: "mid", dst: "data", type: "imports" },
        { src: "top", dst: "mid", type: "calls" },
        { src: "top", dst: "data", type: "imports" },
      ],
    });
    expect(got.byId.data).toMatchObject({ layer: 0, cycle: false, tag: "L0" });
    expect(got.byId.mid).toMatchObject({ layer: 1, tag: "L1" });
    expect(got.byId.app).toMatchObject({ layer: 1, tag: "L1" });
    // Longest path, not the short edge straight to data.
    expect(got.byId.top).toMatchObject({ layer: 2, tag: "L2" });
    expect(got.areas.map((a) => a.id)).toEqual(["data", "app", "mid", "top"]);
  });

  it("flags a 2-area cycle as one layer and parks tests and docs in support", () => {
    const got = deriveLayers({
      areas: ["a", "b", "data", "docs", "tests"],
      edges: [
        { src: "a", dst: "b", type: "imports" },
        { src: "b", dst: "a", type: "calls" },
        { src: "a", dst: "data", type: "imports" },
        { src: "a", dst: "docs", type: "references" },
        { src: "data", dst: "tests", type: "tested_by" },
      ],
    });
    expect(got.byId.data).toMatchObject({ layer: 0, cycle: false, tag: "L0" });
    expect(got.byId.a.cycle).toBe(true);
    expect(got.byId.b.cycle).toBe(true);
    expect(got.byId.a.layer).toBe(got.byId.b.layer);
    expect(got.byId.a.tag).toBe("cycle");
    expect(got.byId.b.tag).toBe("cycle");
    expect(got.byId.docs).toMatchObject({ layer: null, tag: "support" });
    expect(got.byId.tests).toMatchObject({ layer: null, tag: "support" });
    expect(got.areas.filter((a) => a.tag === "support").map((a) => a.id)).toEqual(["docs", "tests"]);
    expect(got.areas.slice(-2).every((a) => a.tag === "support")).toBe(true);
  });
});

describe("fixtures", () => {
  it("ports sparse from the design and builds the dense hairball", () => {
    expect(sparse.nodes.length).toBeGreaterThan(2000);
    expect(sparse.nodes.length).toBeLessThan(2500);
    expect(dense.nodes).toHaveLength(2500);
    expect(dense.edges).toHaveLength(25000);
    const cross = dense.edges.filter((e) => denseArea(e.src) !== denseArea(e.dst)).length;
    expect(cross).toBe(7500);
  });

  it("shuffling node and edge order does not change areas or layers", () => {
    for (const map of [sparse, dense]) {
      const areas = areasAtScale(map.nodes, map.owns);
      const layers = deriveLayers(buildAreaGraph(areas.areas, map.edges));
      const shuffledNodes = shuffle(map.nodes, 3);
      const shuffledOwns = shuffle(map.owns, 4);
      const shuffledEdges = shuffle(map.edges, 5);
      const again = areasAtScale(shuffledNodes, shuffledOwns);
      expect(again).toEqual(areas);
      expect(deriveLayers(buildAreaGraph(again.areas, shuffledEdges))).toEqual(layers);
    }
  });
});
