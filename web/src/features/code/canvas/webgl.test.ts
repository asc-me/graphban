import { describe, expect, it, afterEach } from "vitest";

import webglSrc from "./webgl.ts?raw";
import type { DrawNode, DrawScene } from "./scene";
import type { Stroke } from "./edges";
import {
  appendStroke,
  createWebGL,
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
    expect(shapeKind(node({ kind: "config" }))).toBe(7);
    // shapeKind 7 falls through to the file circle unless the shader has the
    // branch. The fragment shader is not compiled in jsdom.
    expect(webglSrc).toContain("kind == 7");
    expect(webglSrc).toContain("sdHexagon");
    expect(shapeKind(node({ kind: "config", square: true }))).toBe(3);
    const hex = nodeExtent(node({ kind: "config", r: 4 }), 1, 1);
    const square = nodeExtent(node({ kind: "doc", r: 4 }), 1, 1);
    expect(hex).toBeGreaterThan(square);
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
    const scene: DrawScene = {
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
      edgesAlpha: 0.85,
      glow: [],
    };
    const fallback = swapToCanvas2D(glCanvas, d2, scene);
    expect(fallback?.kind).toBe("canvas2d");
    expect(calls).toBeGreaterThan(0);
    expect(d2.width).toBe(80);
    expect(d2.hidden).toBe(false);
    expect(glCanvas.hidden).toBe(true);
  });
});

function fakeGL(
  dynamic: { n: number },
  draws?: { buf: object | null }[],
): WebGL2RenderingContext {
  let bound: object | null = null;
  let vao: { instance?: object | null } | null = null;
  const gl = {
    VERTEX_SHADER: 0x8b31,
    FRAGMENT_SHADER: 0x8b30,
    COMPILE_STATUS: 0x8b81,
    LINK_STATUS: 0x8b82,
    ARRAY_BUFFER: 0x8892,
    STATIC_DRAW: 0x88e4,
    DYNAMIC_DRAW: 0x88e8,
    FLOAT: 0x1406,
    TRIANGLES: 4,
    BLEND: 0x0be2,
    SRC_ALPHA: 0x0302,
    ONE_MINUS_SRC_ALPHA: 0x0303,
    COLOR_BUFFER_BIT: 0x4000,
    DEPTH_TEST: 0x0b71,
    createShader: () => ({}),
    shaderSource() {},
    compileShader() {},
    getShaderParameter: () => true,
    getShaderInfoLog: () => "",
    deleteShader() {},
    createProgram: () => ({}),
    attachShader() {},
    linkProgram() {},
    getProgramParameter: () => true,
    getProgramInfoLog: () => "",
    deleteProgram() {},
    getUniformLocation: () => ({}),
    createBuffer: () => ({}),
    deleteBuffer() {},
    createVertexArray: () => ({}),
    deleteVertexArray() {},
    bindBuffer(_target: number, buf: object | null) {
      bound = buf;
    },
    bindVertexArray(next: { instance?: object | null } | null) {
      vao = next;
    },
    bufferData(_target: number, _data: ArrayBufferView, usage: number) {
      if (usage === 0x88e8) dynamic.n += 1;
    },
    enableVertexAttribArray() {},
    vertexAttribPointer() {
      // The attribute keeps the buffer that was bound when the pointer was set.
      // A later bindBuffer does not retarget it, which is what a shared slot would hide.
      if (vao) vao.instance = bound;
    },
    vertexAttribDivisor() {},
    enable() {},
    disable() {},
    blendFunc() {},
    useProgram() {},
    uniform1f() {},
    uniform2f() {},
    uniform3f() {},
    viewport() {},
    clearColor() {},
    clear() {},
    drawArraysInstanced() {
      draws?.push({ buf: vao?.instance ?? null });
    },
    isContextLost: () => false,
  };
  return gl as unknown as WebGL2RenderingContext;
}

function drawn(): DrawScene {
  return {
    width: 80,
    height: 40,
    dpr: 1,
    camera: { x: 0, y: 0, k: 1 },
    nodes: [node({ x: 4, y: 6 })],
    strokes: [{ ...line }],
    hulls: [],
    rings: [],
    lod: "edges",
    caption: "",
    areasOnly: false,
    revealed: null,
    glow: [],
    edgesAlpha: 0.85,
  };
}

describe("pan upload", () => {
  it("skips the instance upload when only the camera moves, and uploads when the picture changes", () => {
    const dynamic = { n: 0 };
    const gl = fakeGL(dynamic);
    HTMLCanvasElement.prototype.getContext = function (type: string) {
      if (type === "webgl2") return gl;
      return null;
    } as typeof original;
    const canvas = document.createElement("canvas");
    const renderer = createWebGL(canvas);
    expect(renderer?.kind).toBe("webgl");
    expect(dynamic.n).toBe(0);
    const scene = drawn();
    renderer!.draw(scene);
    const uploaded = dynamic.n;
    expect(uploaded).toBeGreaterThan(0);
    renderer!.draw({ ...scene, camera: { x: 12, y: -4, k: 1.02 } });
    expect(dynamic.n).toBe(uploaded);
    renderer!.draw({ ...scene, edgesAlpha: 0.2 });
    expect(dynamic.n).toBeGreaterThan(uploaded);
    const afterAlpha = dynamic.n;
    renderer!.draw({
      ...scene,
      strokes: [{ ...line, x0: 3 }],
    });
    expect(dynamic.n).toBeGreaterThan(afterAlpha);
  });

  it("uploads a presence cloud once and keeps that buffer across a pan", () => {
    const dynamic = { n: 0 };
    const draws: { buf: object | null }[] = [];
    const gl = fakeGL(dynamic, draws);
    HTMLCanvasElement.prototype.getContext = function (type: string) {
      if (type === "webgl2") return gl;
      return null;
    } as typeof original;
    const canvas = document.createElement("canvas");
    const renderer = createWebGL(canvas);
    const scene = drawn();
    renderer!.draw(scene);
    const bare = dynamic.n;
    const cloud = {
      x: 4,
      y: 6,
      r: 20,
      color: "#c6f24e",
      userId: "ada",
      predicted: false,
      alpha: 0.16,
    };
    renderer!.draw({ ...scene, clouds: [cloud] });
    expect(dynamic.n).toBeGreaterThan(bare);
    const uploaded = dynamic.n;
    draws.length = 0;
    renderer!.draw({ ...scene, clouds: [cloud], camera: { x: 9, y: -3, k: 1 } });
    expect(dynamic.n).toBe(uploaded);
    // First instanced draw is the cloud. The last is the node. A shared buffer
    // would make a pan redraw the cloud as the node.
    expect(draws.length).toBeGreaterThan(1);
    expect(draws[0].buf).not.toBe(draws[draws.length - 1].buf);
    expect(draws[0].buf).toBeTruthy();
  });
});
