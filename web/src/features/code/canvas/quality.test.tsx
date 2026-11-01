/**
 * Drawing quality on the live map (reported after the canvas shipped to every size):
 * rough, stair-stepped links; a flat lime slab for the focus glow; highlights that snapped
 * instead of fading; a Map view that could not fit; and Map links covered by their tiles.
 *
 * Each block pins one of those, against the design (Build.dc.html) where it says.
 */

import { act, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { sparse } from "@/lib/graph/__fixtures__/bigMap";
import { clustersLayout, mapLayout, type ClusterLayout, type Hull } from "@/lib/graph/bigLayout";

import { CANVAS_MIN_K, fitCamera, fitScale, zoomFloor } from "./camera";
import type { Stroke } from "./edges";
import { quadraticBetween } from "./edges";
import { FADE_MS, blendAlpha, useAlphaFade } from "./fade";
import { createCanvas2D } from "./canvas2d";
import { GLOW_CORE_PX, buildScene, writeAlpha, type DrawScene } from "./scene";
import { LINE_FLOATS, bakeLines, quadSteps } from "./webgl";

const curve: Stroke = {
  kind: "quad",
  x0: 0,
  y0: 0,
  cx: 50,
  cy: 80,
  x1: 100,
  y1: 0,
  color: "#7ca2ff",
  widthPx: 2,
  label: "",
};

describe("links are one continuous stroke", () => {
  it("joins a curve's segments end to end, sharing the offset at each joint", () => {
    // Segments that each ran half a pixel past their ends overlapped at every joint, and a
    // translucent stroke blended twice there: a bead at each joint along the curve.
    const data = bakeLines([curve], 1, 0.5);
    const steps = quadSteps(curve, 1);
    expect(data.length).toBe(steps * LINE_FLOATS);
    for (let i = 0; i + 1 < steps; i++) {
      const a = i * LINE_FLOATS;
      const b = (i + 1) * LINE_FLOATS;
      // This segment ends where the next begins.
      expect(data[a + 2]).toBeCloseTo(data[b + 0], 5);
      expect(data[a + 3]).toBeCloseTo(data[b + 1], 5);
      // And both use one offset at the joint, so their edges meet instead of crossing.
      expect(data[a + 11]).toBeCloseTo(data[b + 9], 5);
      expect(data[a + 12]).toBeCloseTo(data[b + 10], 5);
    }
  });

  it("starts and ends the curve exactly at its endpoints", () => {
    const data = bakeLines([curve], 1, 0.5);
    const last = (quadSteps(curve, 1) - 1) * LINE_FLOATS;
    expect([data[0], data[1]]).toEqual([0, 0]);
    expect(data[last + 2]).toBeCloseTo(100, 5);
    expect(data[last + 3]).toBeCloseTo(0, 5);
  });
});

function layoutWithEdge(): ClusterLayout {
  return clustersLayout({
    nodes: [
      { id: "a/x.py", path: "a/x.py", kind: "file" },
      { id: "b/y.py", path: "b/y.py", kind: "file" },
    ],
    edges: [{ src: "a/x.py", dst: "b/y.py", type: "imports" }],
  });
}

function sceneFor(layout: ClusterLayout, selectedId: string | null): DrawScene {
  return buildScene(layout, {
    camera: fitCamera(layout.areas.map((a) => ({ x: a.cx, y: a.cy, r: a.r })), 900, 560),
    width: 900,
    height: 560,
    dpr: 1,
    alpha: writeAlpha(layout.ids.length, null, false),
    types: null,
    isolate: null,
    selectedId,
    hoverId: null,
  });
}

describe("the focus glow is the design's", () => {
  it("is a curve at the core width, in the edge's colour", () => {
    // Build.dc.html gGlow: a quadratic, 1.3px core, edge-type colour. It was a straight
    // 2.5px line over an 18.5px lime slab.
    const glow = sceneFor(layoutWithEdge(), "a/x.py").glow;
    expect(glow).toHaveLength(1);
    expect(glow[0].kind).toBe("quad");
    expect(glow[0].widthPx).toBe(GLOW_CORE_PX);
    const mid = { x: (glow[0].x0 + glow[0].x1) / 2, y: (glow[0].y0 + glow[0].y1) / 2 };
    expect(Math.hypot(glow[0].cx - mid.x, glow[0].cy - mid.y)).toBeGreaterThan(1);
  });

  it("draws under the nodes on Canvas 2D, so a node covers the ends of its glow", () => {
    const calls: string[] = [];
    const ctx = new Proxy({} as CanvasRenderingContext2D, {
      get: (_t, key: string) =>
        key === "canvas" ? undefined : (..._a: unknown[]) => calls.push(key),
      set: () => true,
    });
    const canvas = { getContext: () => ctx, width: 0, height: 0 } as unknown as HTMLCanvasElement;
    const renderer = createCanvas2D(canvas)!;
    const scene = sceneFor(layoutWithEdge(), "a/x.py");
    // Nodes on screen, or "arc" below is only the selection ring, which is drawn last anyway.
    expect(scene.nodes.length).toBe(2);
    renderer.draw(scene);
    // Area bundles are curves too, and they come first anyway. The LAST curve is the
    // glow's, and it must still land before the first node is drawn.
    const lastCurve = calls.lastIndexOf("quadraticCurveTo");
    const firstArc = calls.indexOf("arc");
    expect(lastCurve).toBeGreaterThan(-1);
    expect(firstArc).toBeGreaterThan(lastCurve);
  });
});

describe("highlights fade instead of snapping", () => {
  let now = 0;
  let queue: FrameRequestCallback[] = [];
  beforeEach(() => {
    now = 0;
    queue = [];
    vi.spyOn(performance, "now").mockImplementation(() => now);
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      queue.push(cb);
      return queue.length;
    });
    vi.stubGlobal("cancelAnimationFrame", () => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });
  const tick = (ms: number) => {
    now += ms;
    const run = queue;
    queue = [];
    act(() => run.forEach((cb) => cb(now)));
  };

  it("blends the buffer part way, and lands on the target", () => {
    const from = Uint8Array.from([255, 255]);
    const to = Uint8Array.from([255, 56]);
    expect(Array.from(blendAlpha(from, to, 0.5))).toEqual([255, 156]);
    expect(blendAlpha(from, to, 1)).toBe(to);
  });

  it("eases a dim in over the fade, then holds the target", () => {
    const lit = Uint8Array.from([255, 255]);
    const dim = Uint8Array.from([255, 56]);
    const { result, rerender } = renderHook(({ a, f }) => useAlphaFade(a, f), {
      initialProps: { a: lit as Uint8Array, f: null as string | null },
    });
    rerender({ a: dim, f: "n0" });
    tick(FADE_MS / 4);
    const partway = result.current.alpha![1];
    expect(partway).toBeLessThan(255);
    expect(partway).toBeGreaterThan(56);
    expect(result.current.mix).toBeGreaterThan(0);
    expect(result.current.mix).toBeLessThan(1);
    tick(FADE_MS);
    expect(result.current.alpha![1]).toBe(56);
    expect(result.current.mix).toBe(1);
  });

  it("jumps under reduced motion", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
    const { result, rerender } = renderHook(({ a, f }) => useAlphaFade(a, f), {
      initialProps: { a: Uint8Array.from([255]) as Uint8Array, f: null as string | null },
    });
    const dim = Uint8Array.from([56]);
    rerender({ a: dim, f: "n0" });
    expect(result.current.alpha![0]).toBe(56);
  });
});

describe("Map fits", () => {
  it("lowers the zoom floor below 0.7 when the whole map needs it, and Fit uses it", () => {
    // The treemap is taller than a short frame at 0.7. A fixed floor left Fit and zoom-out
    // stuck at 0.7, showing part of the map.
    const map = mapLayout({ nodes: sparse.nodes, edges: [...sparse.edges, ...sparse.owns] });
    const targets = map.areas.map((a) => ({ x: a.cx, y: a.cy, r: a.r }));
    const raw = fitScale(targets, 900, 400)!;
    expect(raw).toBeLessThan(CANVAS_MIN_K);
    const floor = zoomFloor(raw);
    expect(floor).toBeCloseTo(raw, 6);
    expect(fitCamera(targets, 900, 400, 48, floor).k).toBeCloseTo(raw, 6);
  });

  it("keeps 0.7 as the floor when the map already fits there", () => {
    expect(zoomFloor(1.4)).toBe(CANVAS_MIN_K);
    expect(zoomFloor(null)).toBe(CANVAS_MIN_K);
    expect(zoomFloor(Number.NaN)).toBe(CANVAS_MIN_K);
  });

  it("packs a tile's nodes below its name band, as the design does", () => {
    const map = mapLayout({ nodes: sparse.nodes, edges: [...sparse.edges, ...sparse.owns] });
    const byArea = new Map(map.areas.map((a) => [a.id, a]));
    for (let i = 0; i < map.ids.length; i++) {
      const rect = byArea.get(map.areaOf[i])!.rect!;
      const band = rect.h > 70 ? 34 : 10;
      expect(map.y[i] - map.r[i]).toBeGreaterThanOrEqual(rect.y + band - 1e-6);
    }
  });
});

describe("Map links", () => {
  const tile = (id: string, x: number, y: number): Hull => ({
    id,
    cx: x + 50,
    cy: y + 50,
    r: 70,
    color: "#fff",
    count: 1,
    label: id,
    rect: { x, y, w: 100, h: 100 },
  });

  it("run between neighbouring tiles' centres, not their touching edges", () => {
    // Rim to rim between tiles that share an edge was a stub a few pixels long.
    const q = quadraticBetween(tile("a", 0, 0), tile("b", 100, 0));
    expect([q.x0, q.y0]).toEqual([50, 50]);
    expect([q.x1, q.y1]).toEqual([150, 50]);
  });

  it("are drawn over the tiles: tile shapes sit under the canvas", async () => {
    const { CodeGraphCanvas } = await import("../CodeGraphCanvas");
    render(
      <CodeGraphCanvas
        nodes={sparse.nodes.slice(0, 40).map((n) => ({ ...n, name: "" }))}
        edges={[]}
        enabledTypes={["imports"]}
        isolate={false}
        selectedId={null}
        onSelect={() => {}}
        truncated={false}
        totalNodes={40}
        frameId={null}
        layout="map"
        onLayout={() => {}}
      />,
    );
    const host = screen.getByTestId("code-graph-canvas");
    const shapes = await screen.findByTestId("hull-shapes");
    const kids = Array.from(host.children);
    expect(kids.indexOf(shapes)).toBeLessThan(kids.indexOf(screen.getByTestId("code-graph-webgl")));
    expect(shapes.querySelector("rect")).toBeTruthy();
  });
});
