/**
 * Canvas path frame (GRPH-1075, PRD-52 D5/D6).
 *
 * The hop list is the path. Building it must not scan the edge table, and the
 * frame that applies it — alpha buffer, hop segments, one scene — stays inside
 * the same 16ms the pan frame is held to on `dense`.
 */

import { describe, expect, it } from "vitest";

import canvasSrc from "@/features/code/CodeGraphCanvas.tsx?raw";
import drawSrc from "@/features/code/canvas/canvas2d.ts?raw";
import { buildScene, type SceneOpts } from "@/features/code/canvas/scene";
import { NON_PATH_EDGE_ALPHA, pathSegments } from "@/features/code/canvas/overlay";
import { dense } from "@/lib/graph/__fixtures__/bigMap";
import { clustersLayout } from "@/lib/graph/bigLayout";
import { pathAlphaBuffer } from "@/lib/graph/pathHighlight";
import type { CodeEdgeType, CodePath } from "@/lib/types";

function sceneOpts(alpha: Uint8Array, edgesAlpha?: number): SceneOpts {
  return {
    camera: { x: 0, y: 0, k: 1 },
    width: 900,
    height: 560,
    dpr: 1,
    alpha,
    types: null,
    isolate: null,
    selectedId: null,
    hoverId: null,
    edgesAlpha,
  };
}

describe("path segments are the hops, drawn with their types", () => {
  const ids = ["a.py", "b.py", "c.py"];
  const x = Float32Array.from([0, 10, 20]);
  const y = Float32Array.from([0, 0, 5]);
  const route: CodePath = {
    a: "a.py",
    b: "c.py",
    found: true,
    missing: [],
    hops: [
      { src: "a.py", dst: "b.py", type: "imports", forward: true },
      { src: "b.py", dst: "c.py", type: "calls", forward: false },
    ],
  };

  it("keeps each hop's type and endpoints", () => {
    const segs = pathSegments(route, ids, x, y);
    expect(segs.map((s) => s.type)).toEqual(["imports", "calls"]);
    expect(segs[0]).toMatchObject({ x0: 0, y0: 0, x1: 10, y1: 0 });
    expect(segs[1].color).toBeTruthy();
  });

  it("skips a hop whose node is not on this layout", () => {
    const partial: CodePath = {
      ...route,
      hops: [{ src: "a.py", dst: "missing.py", type: "imports", forward: true }],
    };
    expect(pathSegments(partial, ids, x, y)).toEqual([]);
  });

  it("draws nothing when the read found no route", () => {
    expect(pathSegments({ ...route, found: false, hops: [] }, ids, x, y)).toEqual([]);
  });
});

describe("the frame after a path lands on dense", () => {
  it("takes no more than 16ms", () => {
    const layout = clustersLayout({
      nodes: dense.nodes.map((n) => ({ id: n.id, path: n.path, kind: n.kind })),
      edges: dense.edges.map((e) => ({ src: e.src, dst: e.dst, type: e.type })),
    });
    const hop = dense.edges.find((e) => e.type !== "owns") ?? dense.edges[0];
    const route: CodePath = {
      a: hop.src,
      b: hop.dst,
      found: true,
      missing: [],
      hops: [{ src: hop.src, dst: hop.dst, type: hop.type as CodeEdgeType, forward: true }],
    };
    // Warm the path that the timed frame will take. The budget is the frame
    // after the result, not the first time this process sees the layout.
    const warm = pathAlphaBuffer(layout.ids, route)!;
    pathSegments(route, layout.ids, layout.x, layout.y);
    buildScene(layout, sceneOpts(warm, NON_PATH_EDGE_ALPHA));

    const t0 = performance.now();
    const alpha = pathAlphaBuffer(layout.ids, route)!;
    const segments = pathSegments(route, layout.ids, layout.x, layout.y);
    const scene = buildScene(layout, sceneOpts(alpha, NON_PATH_EDGE_ALPHA));
    const dt = performance.now() - t0;

    expect(segments).toHaveLength(1);
    expect(segments[0].type).toBe(hop.type);
    expect(scene.edgesAlpha).toBe(NON_PATH_EDGE_ALPHA);
    const on = layout.ids.indexOf(hop.src);
    const off = layout.ids.findIndex((id) => id !== hop.src && id !== hop.dst);
    expect(scene.nodes.find((n) => n.index === on)?.alpha).toBe(1);
    expect(scene.nodes.find((n) => n.index === off)?.alpha).toBeLessThan(0.3);
    expect(dt).toBeLessThanOrEqual(16);
  });
});

describe("the path frame is applied by the canvas, not only computed", () => {
  it("sets one batch alpha and does not decide inside the edge loop", () => {
    const body = drawSrc.slice(drawSrc.indexOf("function drawEdges"), drawSrc.indexOf("export function createCanvas2D"));
    const loopStart = body.indexOf("for (");
    const loopEnd = body.indexOf("ctx.globalAlpha = 1");
    const loop = body.slice(loopStart, loopEnd);
    expect(body).toContain("scene.edgesAlpha");
    expect(loopStart).toBeGreaterThan(body.indexOf("scene.edgesAlpha"));
    expect(loop).not.toContain("globalAlpha");
  });

  it("the canvas writes the path buffer and passes it as the batch alpha", () => {
    expect(canvasSrc).toContain("pathAlphaBuffer");
    expect(canvasSrc).toContain("pathSegments(");
    expect(canvasSrc).toContain("edgesAlpha: pathAlpha ? NON_PATH_EDGE_ALPHA");
  });
});
