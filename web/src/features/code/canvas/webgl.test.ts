import { describe, expect, it, afterEach } from "vitest";

import type { DrawNode, DrawScene } from "./scene";
import type { Stroke } from "./edges";
import {
  appendStroke,
  diffBitmaps,
  groupStrokes,
  nodeExtent,
  parseHex,
  quadSteps,
  shapeKind,
  swapToCanvas2D,
} from "./webgl";

const line: Stroke = {
  kind: "line",
  x0: 0,
  y0: 0,
  cx: 0,
  cy: 0,
  x1: 10,
  y1: 0,
  color: "#7ca2ff",
  widthPx: 2,
  label: "a",
};

const quad: Stroke = {
  kind: "quad",
  x0: 0,
  y0: 0,
  cx: 5,
  cy: 8,
  x1: 10,
  y1: 0,
  color: "#c6f24e",
  widthPx: 3,
  label: "b",
};

function node(partial: Partial<DrawNode>): DrawNode {
  return {
    id: "n",
    index: 0,
    x: 0,
    y: 0,
    r: 6,
    color: "#7ca2ff",
    kind: "file",
    alpha: 1,
    square: false,
    ...partial,
  };
}

const original = HTMLCanvasElement.prototype.getContext;

afterEach(() => {
  HTMLCanvasElement.prototype.getContext = original;
});

describe("webgl raster inputs", () => {
  it("parses hex and encodes kind the way the fragment shader branches", () => {
    expect(parseHex("#7ca2ff")).toEqual([124 / 255, 162 / 255, 255 / 255]);
    expect(parseHex("#abc")).toEqual([170 / 255, 187 / 255, 204 / 255]);
    expect(shapeKind(node({ kind: "file" }))).toBe(0);
    expect(shapeKind(node({ kind: "symbol" }))).toBe(1);
    expect(shapeKind(node({ kind: "module" }))).toBe(2);
    expect(shapeKind(node({ kind: "file", square: true }))).toBe(3);
    expect(shapeKind(node({ kind: "doc" }))).toBe(3);
    expect(nodeExtent(node({ square: true, r: 4 }), 1, 1)).toBeGreaterThan(4);
  });

  it("batches strokes per colour and tessellates quadratics", () => {
    expect(quadSteps(quad, 1)).toBeGreaterThanOrEqual(8);
    expect(quadSteps(quad, 1)).toBeLessThanOrEqual(48);
    const blueQuad = { ...quad, color: "#7ca2ff" };
    const runs = groupStrokes([line, { ...line, x0: 1 }, quad, blueQuad], 1);
    const by = new Map(runs.map((r) => [r.color, r.data.length / 5]));
    // Two straight segments plus the tessellated copy of the quadratic.
    expect(by.get("#7ca2ff")).toBe(2 + quadSteps(blueQuad, 1));
    expect(by.get("#c6f24e")).toBe(quadSteps(quad, 1));
    const buf: number[] = [];
    appendStroke(buf, line, 1);
    expect(buf).toHaveLength(5);
  });

  it("scores identical bitmaps at zero and a blank frame as a miss", () => {
    const w = 48;
    const h = 32;
    const a = new Uint8ClampedArray(w * h * 4);
    a.fill(10);
    const same = diffBitmaps(a, a.slice(), w, h);
    expect(same.blockMae).toBe(0);
    const blank = new Uint8ClampedArray(w * h * 4);
    const miss = diffBitmaps(a, blank, w, h);
    expect(miss.blockMae).toBeGreaterThan(5);
    expect(miss.inkA).toBe(0);
  });
});

describe("context loss", () => {
  it("paints the last scene on the 2D canvas and hides the lost WebGL canvas", () => {
    let calls = 0;
    HTMLCanvasElement.prototype.getContext = function (type: string) {
      if (type !== "2d") return null;
      return new Proxy({} as CanvasRenderingContext2D, {
        get: () => () => {
          calls += 1;
        },
      });
    } as typeof original;
    const glCanvas = document.createElement("canvas");
    const d2 = document.createElement("canvas");
    d2.hidden = true;
    const scene = {
      width: 80,
      height: 40,
      dpr: 1,
      camera: { x: 0, y: 0, k: 1 },
      nodes: [],
      strokes: [],
      hulls: [],
      rings: [],
      lod: "edges",
      caption: "",
      areasOnly: false,
      revealed: null,
      glow: [],
    } as DrawScene;
    const fallback = swapToCanvas2D(glCanvas, d2, scene);
    expect(fallback?.kind).toBe("canvas2d");
    expect(calls).toBeGreaterThan(0);
    expect(d2.width).toBe(80);
    expect(d2.hidden).toBe(false);
    expect(glCanvas.hidden).toBe(true);
  });
});
