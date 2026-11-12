/**
 * Canvas path frame (GRPH-1075, PRD-52 D5/D6).
 *
 * The hop list is the path. Building it must not scan the edge table, and the
 * frame that applies it — alpha buffer, hop segments, one scene — stays inside
 * the same 16ms the pan frame is held to on `dense`: asserted as a cost relative to a
 * plain frame, so host load cannot fail it (GRPH-1536).
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
  // Relative, not absolute (GRPH-1536). The claim is that applying a path costs about
  // what a plain pan frame costs (no edge scan, no per-edge decision), which is what keeps
  // it inside the pan frame's 16ms. A bare `dt <= 16` broke on loaded shared CI hosts
  // with nothing wrong (18.5ms seen), so both frames are timed here, on the same layout
  // in the same process, best of several runs: host load inflates both sides. A path
  // frame is about 1.4x a plain one on a dev machine, so 2x + 1ms is generous headroom,
  // and an O(nodes^2) or edge-table scan is many times over it.
  it("costs about what a plain frame does", () => {
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
    const plainAlpha = new Uint8Array(layout.ids.length).fill(255);
    const best = (frame: () => void) => {
      frame(); // warm: the budget is the frame after the result, not the first sighting
      let min = Infinity;
      for (let i = 0; i < 7; i++) {
        const t0 = performance.now();
        frame();
        min = Math.min(min, performance.now() - t0);
      }
      return min;
    };

    let alpha: Uint8Array = plainAlpha;
    let segments: ReturnType<typeof pathSegments> = [];
    let scene!: ReturnType<typeof buildScene>;
    const plain = best(() => {
      buildScene(layout, sceneOpts(plainAlpha));
    });
    const path = best(() => {
      alpha = pathAlphaBuffer(layout.ids, route)!;
      segments = pathSegments(route, layout.ids, layout.x, layout.y);
      scene = buildScene(layout, sceneOpts(alpha, NON_PATH_EDGE_ALPHA));
    });

    expect(segments).toHaveLength(1);
    expect(segments[0].type).toBe(hop.type);
    expect(scene.edgesAlpha).toBe(NON_PATH_EDGE_ALPHA);
    const on = layout.ids.indexOf(hop.src);
    const off = layout.ids.findIndex((id) => id !== hop.src && id !== hop.dst);
    expect(scene.nodes.find((n) => n.index === on)?.alpha).toBe(1);
    expect(scene.nodes.find((n) => n.index === off)?.alpha).toBeLessThan(0.3);
    expect(path).toBeLessThanOrEqual(2 * plain + 1);
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
