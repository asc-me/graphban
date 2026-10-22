/**
 * Canvas-mode core (GRPH-1071). The numbers in the acceptance text are pinned
 * here: 2.6, 6,000, 140. Changing the constant, or the call that consults it,
 * fails one of these.
 */

import { renderHook, act } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import viewSrc from "../../features/code/CodeGraphView.tsx?raw";

import { dense, sparse } from "./__fixtures__/bigMap";
import {
  clustersLayout,
  GRID_CELL,
  HIT_WINDOW,
  hitTest,
  hitVisits,
  layoutRunCount,
  nodeRadius,
  resetLayoutRuns,
  bundleLabel,
  type ClusterLayout,
  type RankedEdge,
} from "./bigLayout";
import { useAreaLayout, workerAfterDeath, type AreaWorker } from "./useAreaLayout";
import { replaceTween, sampleTween, TWEEN_MS } from "@/features/code/canvas/camera";
import { createCanvas2D } from "@/features/code/canvas/canvas2d";
import type { DrawScene } from "@/features/code/canvas/scene";
import {
  EDGE_CAP,
  LOD_ZOOM,
  bundleStrokes,
  capCaption,
  edgeLod,
  hitBundle,
  quadPoint,
  rankAndCap,
} from "@/features/code/canvas/edges";
import { LABEL_CAP, pickLabels, type LabelNode } from "@/features/code/canvas/labels";
import {
  alphaWriteCount,
  buildScene,
  resetAlphaWrites,
  type SceneOpts,
} from "@/features/code/canvas/scene";

function pair(): { nodes: { id: string; path: string; kind: string }[]; edges: { src: string; dst: string; type: string }[] } {
  const nodes = [
    { id: "alpha", path: "alpha", kind: "module" },
    { id: "beta", path: "beta", kind: "module" },
  ];
  const edges: { src: string; dst: string; type: string }[] = [];
  for (let i = 0; i < 10; i++) {
    nodes.push({ id: `alpha/f${i}`, path: `alpha/f${i}.py`, kind: "file" });
    nodes.push({ id: `beta/f${i}`, path: `beta/f${i}.py`, kind: "file" });
    edges.push({ src: "alpha", dst: `alpha/f${i}`, type: "owns" });
    edges.push({ src: "beta", dst: `beta/f${i}`, type: "owns" });
    edges.push({ src: `alpha/f${i}`, dst: `beta/f${i}`, type: "imports" });
  }
  return { nodes, edges };
}

function placed(ids: string[]): string {
  return ids.slice().sort().join("|");
}

function sig(layout: ClusterLayout): string {
  const rows = layout.ids.map(
    (id, i) => `${id}@${Math.round(layout.x[i] * 1000)}:${Math.round(layout.y[i] * 1000)}`,
  );
  return rows.sort().join("|");
}

function sceneOpts(k: number, extra?: Partial<SceneOpts>): SceneOpts {
  return {
    camera: { x: 0, y: 0, k },
    width: 900,
    height: 560,
    dpr: 1,
    alpha: new Uint8Array(8),
    types: null,
    isolate: null,
    selectedId: null,
    hoverId: null,
    ...extra,
  };
}

describe("clusters layout", () => {
  it("is identical across two runs and on shuffled input", () => {
    const first = clustersLayout(pair());
    const second = clustersLayout(pair());
    expect(sig(first)).toBe(sig(second));
    expect(placed(first.ids)).toBe(placed(second.ids));

    const g = pair();
    const shuffled = clustersLayout({
      nodes: [...g.nodes].reverse(),
      edges: [...g.edges].reverse(),
    });
    expect(sig(shuffled)).toBe(sig(first));
  });

  it("does not recompute positions while a frame is built", () => {
    resetLayoutRuns();
    const layout = clustersLayout(pair());
    const runs = layoutRunCount();
    buildScene(layout, sceneOpts(1, { alpha: new Uint8Array(layout.ids.length) }));
    buildScene(layout, sceneOpts(2.7, { alpha: new Uint8Array(layout.ids.length) }));
    expect(layoutRunCount()).toBe(runs);
    resetAlphaWrites();
    buildScene(layout, sceneOpts(1.2, { alpha: new Uint8Array(layout.ids.length) }));
    expect(alphaWriteCount()).toBe(0);
  });

  it("hit-tests one 4×4 window, not every node", () => {
    const layout: ClusterLayout = {
      ids: ["near", "far"],
      x: Float32Array.from([5, 500]),
      y: Float32Array.from([5, 500]),
      r: Float32Array.from([4, 4]),
      kinds: ["file", "file"],
      colors: ["#fff", "#fff"],
      areaOf: ["a", "b"],
      areas: [],
      bundles: [],
      edges: [],
      grid: { "0,0": [0], "50,50": [1] },
      depth: null,
      nodeCount: 2,
    };
    expect(hitTest(layout, 5, 5)).toBe("near");
    expect(hitVisits()).toBe(1);
    expect(hitTest(layout, 500, 500)).toBe("far");
    expect(hitVisits()).toBe(1);
  });

  it("scales member radii with the fit so rings do not collapse", () => {
    // The frame scale multiplies positions. Radii that stay at the pre-fit
    // size overlap: on sparse, most nodes then intersect a neighbour by more
    // than 2 units, and dense overlaps every node. Zoom cannot separate that
    // — the overlap is in world space.
    const cap = nodeRadius("module", Number.POSITIVE_INFINITY);
    for (const map of [sparse, dense]) {
      const layout = clustersLayout({ nodes: map.nodes, edges: map.edges });
      expect(layout.nodeCount).toBe(map.nodes.length);
      let maxR = 0;
      for (let i = 0; i < layout.ids.length; i++) maxR = Math.max(maxR, layout.r[i]);
      expect(maxR).toBeLessThanOrEqual(cap + 1e-3);
      expect(pairsCloserThan(layout, 2)).toBe(0);
    }
  });

  it("hits a large module from both sides of its disc, inside a 4×4 window", () => {
    // r=12.6 at x=9.9. A window pinned at (ix-1)..(ix+2) misses the point
    // 14 units to the right (cell 0 is outside that block) and hits from the
    // left. The disc of radius 12.6+slack touches cell 0 from either side.
    const x = 9.9;
    const r = 12.6;
    const cell = `${Math.floor(x / GRID_CELL)},${Math.floor(0 / GRID_CELL)}`;
    const layout = oneNode("mod", x, 0, r, { [cell]: [0] });
    expect(hitTest(layout, x + 14, 0)).toBe("mod");
    expect(hitTest(layout, x - 14, 0)).toBe("mod");
    expect(hitTest(layout, x + 16, 0)).toBeNull();

    const grid: Record<string, number[]> = {};
    const ids: string[] = [];
    const xs: number[] = [];
    const ys: number[] = [];
    const rs: number[] = [];
    let n = 0;
    for (let cx = -3; cx <= 6; cx++) {
      for (let cy = -3; cy <= 6; cy++) {
        grid[`${cx},${cy}`] = [n];
        ids.push(`${cx},${cy}`);
        xs.push(cx * GRID_CELL + 1);
        ys.push(cy * GRID_CELL + 1);
        rs.push(1);
        n += 1;
      }
    }
    hitTest(
      {
        ...oneNode("mod", x, 0, r, grid),
        ids,
        x: Float32Array.from(xs),
        y: Float32Array.from(ys),
        r: Float32Array.from(rs),
        nodeCount: n,
      },
      x,
      0,
    );
    expect(hitVisits()).toBeLessThanOrEqual(HIT_WINDOW * HIT_WINDOW);
  });
});

function pairsCloserThan(layout: ClusterLayout, penetration: number): number {
  let n = 0;
  const { x, y, r } = layout;
  for (let i = 0; i < layout.ids.length; i++) {
    for (let j = i + 1; j < layout.ids.length; j++) {
      const reach = r[i] + r[j] - penetration;
      if (reach <= 0) continue;
      const dx = x[i] - x[j];
      if (Math.abs(dx) >= reach) continue;
      const dy = y[i] - y[j];
      if (Math.abs(dy) >= reach) continue;
      if (dx * dx + dy * dy < reach * reach) n += 1;
    }
  }
  return n;
}

function oneNode(
  id: string,
  x: number,
  y: number,
  r: number,
  grid: Record<string, number[]>,
): ClusterLayout {
  return {
    ids: [id],
    x: Float32Array.from([x]),
    y: Float32Array.from([y]),
    r: Float32Array.from([r]),
    kinds: ["module"],
    colors: ["#fff"],
    areaOf: ["a"],
    areas: [],
    bundles: [],
    edges: [],
    grid,
    depth: null,
    nodeCount: 1,
  };
}

describe("edge level of detail", () => {
  it("switches at 2.6× and caps at 6,000 with the canonical caption", () => {
    expect(LOD_ZOOM).toBe(2.6);
    expect(EDGE_CAP).toBe(6000);
    expect(edgeLod(2.599)).toBe("bundles");
    expect(edgeLod(2.6)).toBe("edges");
    expect(capCaption(6000, 9412)).toBe(
      "6,000 of 9,412 edges in view: densest first. Zoom in, filter types, or Isolate.",
    );

    const n = 7000;
    const edges: RankedEdge[] = Array.from({ length: n }, (_, i) => ({
      src: "a",
      dst: "b",
      type: i % 5 === 0 ? "calls" : "imports",
      index: i,
      srcArea: "A",
      dstArea: "B",
    }));
    const layout: ClusterLayout = {
      ids: ["a", "b"],
      x: Float32Array.from([100, 400]),
      y: Float32Array.from([100, 100]),
      r: Float32Array.from([8, 8]),
      kinds: ["file", "file"],
      colors: ["#7ca2ff", "#c6f24e"],
      areaOf: ["A", "B"],
      areas: [
        { id: "A", cx: 100, cy: 100, r: 40, color: "#111111", count: 1, label: "A" },
        { id: "B", cx: 400, cy: 100, r: 40, color: "#222222", count: 1, label: "B" },
      ],
      bundles: [],
      edges,
      grid: {},
      depth: 1,
      nodeCount: 2,
    };
    const alpha = new Uint8Array(2);
    alpha.fill(255);
    const high = buildScene(layout, sceneOpts(2.6, { alpha }));
    expect(high.lod).toBe("edges");
    expect(high.strokes).toHaveLength(6000);
    expect(high.caption).toBe(capCaption(6000, 7000));
    expect(high.glow).toHaveLength(0);

    const focused = buildScene(layout, sceneOpts(2.6, { alpha, selectedId: "a" }));
    expect(focused.glow).toHaveLength(320);

    const low = buildScene(layout, sceneOpts(2.599, { alpha }));
    expect(low.lod).toBe("bundles");
    expect(low.strokes.every((s) => s.kind === "quad" || low.strokes.length === 0)).toBe(true);

    const filtered = rankAndCap(edges, {
      types: new Set(["calls"]),
      isolate: null,
      inView: () => true,
      cap: EDGE_CAP,
    });
    expect(filtered.kept.every((e) => e.type === "calls")).toBe(true);
    expect(filtered.kept.length).toBeLessThan(edges.length);

    const isolated = rankAndCap(
      [
        { src: "a", dst: "b", type: "imports", index: 0, srcArea: "A", dstArea: "B" },
        { src: "a", dst: "c", type: "imports", index: 1, srcArea: "A", dstArea: "C" },
      ],
      { types: null, isolate: new Set(["a", "b"]), inView: () => true, cap: EDGE_CAP },
    );
    expect(isolated.kept.map((e) => e.dst)).toEqual(["b"]);
  });

  it("draws two offset strokes for a 60/40 split and one stroke at 80%", () => {
    const hullA = { id: "A", cx: 0, cy: 0, r: 40, color: "#111111", count: 10, label: "A" };
    const hullB = { id: "B", cx: 200, cy: 0, r: 40, color: "#222222", count: 10, label: "B" };
    const split = bundleStrokes(
      {
        a: "A",
        b: "B",
        ab: 60,
        ba: 40,
        colorA: hullA.color,
        colorB: hullB.color,
        label: bundleLabel("A", "B", 60, 40),
        hullA,
        hullB,
      },
      1,
    );
    expect(split).toHaveLength(2);
    expect(split[0].color).not.toBe(split[1].color);
    expect(split[0].label).toBe("A→B 60 · B→A 40");
    expect(split[1].label).toBe(split[0].label);
    const gap = Math.abs(split[0].y0 - split[1].y0);
    expect(gap).toBeGreaterThan(split[0].widthPx / 2 + split[1].widthPx / 2);

    const dominant = bundleStrokes(
      { ...{
        a: "A", b: "B", ab: 80, ba: 20,
        colorA: hullA.color, colorB: hullB.color,
        label: bundleLabel("A", "B", 80, 20), hullA, hullB,
      } },
      1,
    );
    expect(dominant).toHaveLength(1);
    expect(dominant[0].marker).toBeUndefined();
  });

  it("hits a long 60/40 bundle along the stroke, not only at 11 samples", () => {
    const hullA = { id: "A", cx: 0, cy: 0, r: 40, color: "#111111", count: 10, label: "A" };
    const hullB = { id: "B", cx: 800, cy: 0, r: 40, color: "#222222", count: 10, label: "B" };
    const strokes = bundleStrokes(
      {
        a: "A",
        b: "B",
        ab: 60,
        ba: 40,
        colorA: hullA.color,
        colorB: hullB.color,
        label: bundleLabel("A", "B", 60, 40),
        hullA,
        hullB,
      },
      1,
    );
    expect(strokes.length).toBeGreaterThan(0);
    expect(strokes[0].label).toBe("A→B 60 · B→A 40");
    const cam = { x: 0, y: 0, k: 1 };
    // t = 0.05 sits halfway between the old fixed samples at 0 and 0.1.
    // On a fitted area pair that gap is wider than the 10px hit radius.
    for (const stroke of strokes) {
      for (const t of [0.05, 0.15, 0.5, 0.85, 0.95]) {
        const p = quadPoint(stroke, t);
        expect(hitBundle(strokes, p.x, p.y, cam)?.label).toBe("A→B 60 · B→A 40");
      }
    }
    const mid = quadPoint(strokes[0], 0.5);
    expect(hitBundle(strokes, mid.x, mid.y + 40, cam)).toBeNull();
  });
});

describe("canvas bitmap scale", () => {
  it("draws nodes, edges, rings and glow in device pixels, not the CSS quarter", () => {
    let matrix = [1, 0, 0, 1, 0, 0];
    const draws: number[][] = [];
    const dpr = 2;
    const camera = { x: 10, y: 20, k: 3 };
    const expected = [dpr * camera.k, 0, 0, dpr * camera.k, dpr * camera.x, dpr * camera.y];
    // Each layer has to set the matrix itself. Inheriting it from the layer
    // above would keep these draws correct after one of them stopped.
    let cameraSets = 0;
    const ctx = new Proxy({} as CanvasRenderingContext2D, {
      get(_target, prop) {
        if (prop === "setTransform") {
          return (a: number, b: number, c: number, d: number, e: number, f: number) => {
            matrix = [a, b, c, d, e, f];
            if (a === expected[0] && b === 0 && c === 0 && d === expected[3] && e === expected[4] && f === expected[5]) {
              cameraSets += 1;
            }
          };
        }
        if (prop === "fill" || prop === "stroke" || prop === "fillRect") {
          return () => {
            draws.push(matrix.slice());
          };
        }
        return () => {};
      },
    });
    const canvas = {
      width: 0,
      height: 0,
      getContext: () => ctx,
    } as unknown as HTMLCanvasElement;
    const scene: DrawScene = {
      width: 200,
      height: 100,
      dpr,
      camera,
      nodes: [
        {
          id: "n",
          index: 0,
          x: 40,
          y: 12,
          r: 6,
          color: "#7ca2ff",
          kind: "file",
          alpha: 1,
          square: false,
        },
      ],
      strokes: [
        {
          kind: "line",
          x0: 0,
          y0: 0,
          cx: 0,
          cy: 0,
          x1: 30,
          y1: 0,
          color: "#8b949e",
          widthPx: 1.25,
          label: "a→b",
        },
      ],
      hulls: [],
      rings: [{ x: 40, y: 12, r: 13, color: "#7ca2ff", kind: "selection" }],
      lod: "edges",
      caption: "",
      areasOnly: false,
      revealed: null,
      glow: [
        {
          kind: "line",
          x0: 0,
          y0: 0,
          cx: 0,
          cy: 0,
          x1: 30,
          y1: 0,
          color: "#c6f24e",
          widthPx: 2.5,
          label: "",
        },
      ],
    };
    const renderer = createCanvas2D(canvas);
    expect(renderer).not.toBeNull();
    renderer!.draw(scene);
    expect(draws.length).toBeGreaterThan(0);
    expect(cameraSets).toBe(4);
    for (const drawn of draws) expect(drawn).toEqual(expected);
    expect(canvas.width).toBe(400);
    expect(canvas.height).toBe(200);
  });
});

describe("labels", () => {
  it("keeps at most 140, never overlapping, and always keeps the selected label", () => {
    expect(LABEL_CAP).toBe(140);
    const nodes: LabelNode[] = Array.from({ length: 200 }, (_, i) => ({
      id: i === 0 ? "selected" : `n${i}`,
      area: "alpha",
      degree: 200 - i,
      inView: true,
      box: { x: (i % 20) * 40, y: Math.floor(i / 20) * 30, w: 20, h: 12 },
    }));
    nodes[1].box = { ...nodes[0].box };
    const kept = pickLabels({
      nodes,
      selectedId: "selected",
      hoverId: null,
      neighbors: [],
      focusKind: "file",
      zoom: 16,
    });
    expect(kept).toHaveLength(140);
    expect(kept[0]).toBe("selected");
    expect(kept).not.toContain("n1");
    const byId = new Map(nodes.map((n) => [n.id, n.box]));
    for (let i = 0; i < kept.length; i++) {
      for (let j = i + 1; j < kept.length; j++) {
        const a = byId.get(kept[i])!;
        const b = byId.get(kept[j])!;
        const overlap = a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
        expect(overlap).toBe(false);
      }
    }
  });
});

describe("a 12,000-node map", () => {
  it("draws areas and bundles only until one area fills the view", () => {
    const n = 12_000;
    const ids = Array.from({ length: n }, (_, i) => `n${i}`);
    const layout: ClusterLayout = {
      ids,
      x: new Float32Array(n),
      y: new Float32Array(n),
      r: new Float32Array(n).fill(4),
      kinds: ids.map(() => "file"),
      colors: ids.map(() => "#ffffff"),
      areaOf: ids.map((_, i) => (i < n / 2 ? "A" : "B")),
      areas: [
        { id: "A", cx: 80, cy: 80, r: 30, color: "#111111", count: n / 2, label: "A" },
        { id: "B", cx: 1400, cy: 80, r: 30, color: "#222222", count: n / 2, label: "B" },
      ],
      bundles: [],
      edges: [],
      grid: {},
      depth: 1,
      nodeCount: n,
    };
    const scene = buildScene(layout, sceneOpts(3, { alpha: new Uint8Array(n) }));
    expect(scene.nodes).toHaveLength(0);
    expect(scene.lod).toBe("areas");
    expect(scene.areasOnly).toBe(true);
    expect(scene.caption).toBe("Large map: zoom into an area to see its files");
  });
});

describe("camera tween", () => {
  it("replaces a running tween instead of queueing one, and jumps when reduced", () => {
    const first = replaceTween({ x: 0, y: 0, k: 1 }, { x: 10, y: 0, k: 2 }, 0, false);
    const next = replaceTween(first.cam, { x: 0, y: 40, k: 7 }, 100, false);
    expect(next.tween?.to).toEqual({ x: 0, y: 40, k: 7 });
    expect(next.tween?.start).toBe(100);
    expect(sampleTween(next.tween!, 100 + TWEEN_MS / 2).done).toBe(false);
    const done = sampleTween(next.tween!, 100 + TWEEN_MS);
    expect(done.done).toBe(true);
    expect(done.cam.k).toBeCloseTo(7);
    const jump = replaceTween({ x: 0, y: 0, k: 1 }, { x: 5, y: 5, k: 3 }, 0, true);
    expect(jump.tween).toBeNull();
    expect(jump.cam).toEqual({ x: 5, y: 5, k: 3 });
  });
});

describe("layout worker", () => {
  it("respawns once and then lays out inline", () => {
    expect(workerAfterDeath(0)).toBe("respawn");
    expect(workerAfterDeath(1)).toBe("inline");
    const workers: AreaWorker[] = [];
    const spawn = (): AreaWorker => {
      const worker: AreaWorker = {
        postMessage: () => {},
        terminate: () => {},
        onmessage: null,
        onerror: null,
      };
      workers.push(worker);
      return worker;
    };
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const input = {
      nodes: [
        { id: "alpha", path: "alpha", kind: "module" },
        { id: "alpha/f", path: "alpha/f.py", kind: "file" },
      ],
      edges: [{ src: "alpha", dst: "alpha/f", type: "owns" }],
    };
    const { result } = renderHook(() => useAreaLayout(input, { spawn }));
    expect(workers).toHaveLength(1);
    act(() => {
      workers[0].onerror?.(new Event("error"));
    });
    expect(workers).toHaveLength(2);
    act(() => {
      workers[1].onerror?.(new Event("error"));
    });
    expect(workers).toHaveLength(2);
    expect(result.current.mode).toBe("inline");
    expect(result.current.layout).not.toBeNull();
    expect(warn).toHaveBeenCalledWith(
      "layout worker died twice; using inline layout for this session",
    );
    warn.mockRestore();
  });
});

describe("the view passes the truncation flag into the canvas", () => {
  it("renders canvas mode from the detail budget and forwards truncated", () => {
    const src = viewSrc;
    expect(src).toContain("allIds.length > DETAIL_BUDGET");
    expect(src).toContain("<CodeGraphCanvas");
    expect(src).toContain("truncated={truncated}");
    const layoutAt = src.indexOf("const layoutEdges = React.useMemo(");
    const memo = src.slice(layoutAt, src.indexOf("]);", layoutAt));
    expect(memo).not.toContain("enabled");
  });
});
