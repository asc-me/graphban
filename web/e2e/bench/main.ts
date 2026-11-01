/**
 * Draws one fixture and reports paint, pan and hit-test times. The page does
 * not mount React: a pan frame only transforms the cached layout.
 *
 * `__bench` defaults to Canvas 2D (the S2 budget). Pass `{ renderer: "webgl" }`
 * for the S10 budget. `__pixelDiff` paints `sparse` with both renderers.
 */

import { createCanvas2D } from "../../src/features/code/canvas/canvas2d";
import { buildScene, type DrawScene } from "../../src/features/code/canvas/scene";
import { diffBitmaps, createWebGL } from "../../src/features/code/canvas/webgl";
import type { Renderer } from "../../src/features/code/canvas/renderer";
import { fitCamera, framePoint } from "../../src/features/code/canvas/camera";
import { clustersLayout, hitTest, type ClusterLayout } from "../../src/lib/graph/bigLayout";
import { dense, sparse, type BigMap } from "../../src/lib/graph/__fixtures__/bigMap";

export interface BenchResult {
  paintMs: number;
  panP95: number;
  hitMs: number;
  nodes: number;
  /** Strokes in the first frame. Zero means the camera missed the graph. */
  edges: number;
}

function mapOf(name: "sparse" | "dense"): BigMap {
  return name === "sparse" ? sparse : dense;
}

function makeRenderer(canvas: HTMLCanvasElement, which: "webgl" | "canvas2d"): Renderer {
  if (which === "webgl") {
    const gl = createWebGL(canvas);
    if (!gl) throw new Error("webgl2 context missing");
    return gl;
  }
  const renderer = createCanvas2D(canvas);
  if (!renderer) throw new Error("canvas 2d context missing");
  return renderer;
}

function bench(name: "sparse" | "dense", opts?: { renderer?: "webgl" | "canvas2d"; zoom?: number }): BenchResult {
  const map = mapOf(name);
  const nodes = map.nodes.map((n) => ({ id: n.id, path: n.path, kind: n.kind }));
  const edges = [...map.owns, ...map.edges];
  const canvas = document.getElementById("c") as HTMLCanvasElement;
  const renderer = makeRenderer(canvas, opts?.renderer ?? "canvas2d");
  const zoom = opts?.zoom ?? 1;

  const t0 = performance.now();
  const layout = clustersLayout({ nodes, edges });
  const alpha = new Uint8Array(layout.ids.length);
  alpha.fill(255);
  const width = 1200;
  const height = 800;
  const base = {
    width,
    height,
    dpr: 1,
    alpha,
    types: null,
    isolate: null,
    selectedId: null,
    hoverId: null,
  };
  // The default camera is the S2 sparse probe. An explicit zoom frames the
  // layout at that scale, so a 2.6× dense run is individual edges, not an
  // empty corner of the map.
  let origin = { x: 40, y: 30, k: zoom };
  if (opts?.zoom != null) {
    const fit = fitCamera(
      layout.areas.map((a) => ({ x: a.cx, y: a.cy, r: a.r })),
      width,
      height,
    );
    const cx = (width / 2 - fit.x) / fit.k;
    const cy = (height / 2 - fit.y) / fit.k;
    origin = framePoint(cx, cy, zoom, width, height);
  }
  const first = buildScene(layout, { ...base, camera: origin });
  renderer.draw(first);
  const paintMs = performance.now() - t0;

  // A pan frame on a shared CI runner can stall for reasons that are not the
  // renderer. Warm-up frames are drawn and thrown away, and enough frames are
  // timed that p95 is not just the second-slowest one.
  const warmup = 5;
  const timed = 120;
  const samples: number[] = [];
  for (let n = 0; n < warmup + timed; n++) {
    // The walk repeats the same 40 cameras, so a longer run does not pan
    // further. The Canvas 2D budget pans at a fixed zoom, the same walk the S2
    // bench already measured. An explicit zoom (dense WebGL at 2.6×) also steps
    // ±0.05, which stays inside the renderer's 8% bake window so the pan does
    // not re-upload instance buffers.
    const i = n % 40;
    const cam = opts?.zoom != null
      ? { x: origin.x + (i % 5) * 6, y: origin.y + (i % 4) * 4, k: zoom + (i % 2) * 0.05 }
      : { x: origin.x + (i + 1) * 4, y: origin.y + (i + 1) * 2, k: zoom };
    const t = performance.now();
    renderer.draw(buildScene(layout, { ...base, camera: cam }));
    if (n >= warmup) samples.push(performance.now() - t);
  }
  samples.sort((a, b) => a - b);
  const panP95 = samples[Math.min(samples.length - 1, Math.floor(samples.length * 0.95))];

  const ht0 = performance.now();
  const probes = 50;
  for (let i = 0; i < probes; i++) hitTest(layout, 80 + i * 3, 60 + i);
  const hitMs = (performance.now() - ht0) / probes;
  renderer.destroy();
  return { paintMs, panP95, hitMs, nodes: layout.nodeCount, edges: first.strokes.length };
}

export interface DprProbe {
  /** Red channel at the CSS center, read in device pixels. */
  correct: number;
  /** Red channel where a camera-only matrix paints the same node. */
  quarter: number;
  bitmap: number;
}

/**
 * One node, drawn at dpr 2. The center in CSS pixels is (140, 100). On the
 * bitmap that is (280, 200) when the camera matrix includes dpr, and (140, 100)
 * when a layer replaces that matrix with the camera alone.
 */
function dprProbe(): DprProbe {
  const canvas = document.getElementById("c") as HTMLCanvasElement;
  const renderer = createCanvas2D(canvas);
  if (!renderer) throw new Error("canvas 2d context missing");
  const dpr = 2;
  const width = 400;
  const height = 300;
  const scene: DrawScene = {
    width,
    height,
    dpr,
    camera: { x: 100, y: 80, k: 1 },
    nodes: [
      {
        id: "n",
        index: 0,
        x: 40,
        y: 20,
        r: 8,
        color: "#ff00ff",
        kind: "file",
        alpha: 1,
        square: false,
      },
    ],
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
  renderer.draw(scene);
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("context lost");
  const cssX = 140;
  const cssY = 100;
  const correct = ctx.getImageData(cssX * dpr, cssY * dpr, 1, 1).data[0];
  const quarter = ctx.getImageData(cssX, cssY, 1, 1).data[0];
  const bitmap = canvas.width;
  renderer.destroy();
  return { correct, quarter, bitmap };
}

export interface PixelDiff {
  blockMae: number;
  ink2d: number;
  inkGl: number;
  centerHit: number;
  centerN: number;
  zoom: number;
  /** Same thresholds, on a scene whose bottom layer is one unpredicted cloud. */
  cloudMae: number;
  cloudInk2d: number;
  cloudInkGl: number;
}

function readWebGL(canvas: HTMLCanvasElement): Uint8ClampedArray {
  const gl = canvas.getContext("webgl2");
  if (!gl) throw new Error("webgl2 context missing");
  const w = canvas.width;
  const h = canvas.height;
  const buf = new Uint8Array(w * h * 4);
  gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, buf);
  const out = new Uint8ClampedArray(w * h * 4);
  const row = w * 4;
  for (let y = 0; y < h; y++) out.set(buf.subarray((h - 1 - y) * row, (h - y) * row), y * row);
  // The WebGL buffer is premultiplied. Canvas 2D getImageData is straight,
  // so undo the multiply before the two rasters are compared. Opaque and
  // empty pixels are already straight.
  for (let i = 0; i < out.length; i += 4) {
    const a = out[i + 3];
    if (a === 0 || a === 255) continue;
    out[i] = Math.min(255, Math.round((out[i] * 255) / a));
    out[i + 1] = Math.min(255, Math.round((out[i + 1] * 255) / a));
    out[i + 2] = Math.min(255, Math.round((out[i + 2] * 255) / a));
  }
  return out;
}

function paintPair(scene: DrawScene): { mae: number; ink2d: number; inkGl: number; img2: Uint8ClampedArray; imgG: Uint8ClampedArray; canvas: HTMLCanvasElement } {
  const c2 = document.createElement("canvas");
  const cg = document.createElement("canvas");
  document.body.append(c2, cg);
  const a = createCanvas2D(c2);
  const b = createWebGL(cg);
  if (!a || !b) throw new Error("both renderers are required for the pixel diff");
  a.draw(scene);
  b.draw(scene);
  const img2 = c2.getContext("2d")!.getImageData(0, 0, c2.width, c2.height).data;
  const imgG = readWebGL(cg);
  const diff = diffBitmaps(img2, imgG, c2.width, c2.height);
  a.destroy();
  b.destroy();
  c2.remove();
  cg.remove();
  return { mae: diff.blockMae, ink2d: diff.inkA, inkGl: diff.inkB, img2, imgG, canvas: c2 };
}

/** One soft cloud, no dashes. Dashes are shared segments and are not what this diff is for. */
function cloudProbe(): DrawScene {
  const layout: ClusterLayout = {
    ids: ["n.py"],
    x: Float32Array.from([40]),
    y: Float32Array.from([30]),
    r: Float32Array.from([6]),
    kinds: ["file"],
    colors: ["#7ca2ff"],
    areaOf: ["a"],
    areas: [],
    bundles: [],
    edges: [],
    grid: {},
    depth: null,
    nodeCount: 1,
  };
  const scene = buildScene(layout, {
    width: 480,
    height: 320,
    dpr: 1,
    camera: { x: 200, y: 130, k: 1 },
    alpha: Uint8Array.from([255]),
    types: null,
    isolate: null,
    selectedId: null,
    hoverId: null,
    clouds: [{ x: 40, y: 30, r: 36, color: "#c6f24e", userId: "ada", predicted: false, alpha: 0.16 }],
  });
  // The node would count as ink on its own. This probe is the cloud alone.
  return { ...scene, nodes: [], strokes: [], rings: [], glow: [] };
}

/** `sparse` through both renderers, then one presence cloud. Node centres and a coarse grid have to agree. */
function pixelDiff(): PixelDiff {
  const map = sparse;
  const nodes = map.nodes.map((n) => ({ id: n.id, path: n.path, kind: n.kind }));
  const edges = [...map.owns, ...map.edges];
  const layout = clustersLayout({ nodes, edges });
  const alpha = new Uint8Array(layout.ids.length);
  alpha.fill(255);
  const base = {
    width: 480,
    height: 320,
    dpr: 1,
    alpha,
    types: null as null,
    isolate: null as null,
    selectedId: null as null,
    hoverId: null as null,
  };
  let zoom = 1;
  let scene = buildScene(layout, { ...base, camera: { x: 20, y: 16, k: zoom } });
  const radii = scene.nodes.map((n) => n.r * zoom).sort((a, b) => a - b);
  const med = radii[Math.floor(radii.length / 2)] ?? 1;
  if (med < 3.5) {
    zoom = Math.min(2.4, 3.5 / Math.max(med, 0.25));
    scene = buildScene(layout, { ...base, camera: { x: 20, y: 16, k: zoom } });
  }
  const painted = paintPair(scene);
  const img2 = painted.img2;
  const imgG = painted.imgG;
  const c2 = painted.canvas;
  let centerHit = 0;
  let centerN = 0;
  const w = c2.width;
  const h = c2.height;
  for (const node of scene.nodes) {
    const sr = node.r * scene.camera.k;
    if (sr < 2) continue;
    const x = Math.round(node.x * scene.camera.k + scene.camera.x);
    const y = Math.round(node.y * scene.camera.k + scene.camera.y);
    if (x < 1 || y < 1 || x >= w - 1 || y >= h - 1) continue;
    centerN += 1;
    const i = (y * w + x) * 4;
    if (
      Math.abs(img2[i] - imgG[i]) < 48 &&
      Math.abs(img2[i + 1] - imgG[i + 1]) < 48 &&
      Math.abs(img2[i + 2] - imgG[i + 2]) < 48
    ) {
      centerHit += 1;
    }
  }
  const cloud = paintPair(cloudProbe());
  return {
    blockMae: painted.mae,
    ink2d: painted.ink2d,
    inkGl: painted.inkGl,
    centerHit,
    centerN,
    zoom,
    cloudMae: cloud.mae,
    cloudInk2d: cloud.ink2d,
    cloudInkGl: cloud.inkGl,
  };
}

declare global {
  interface Window {
    __bench: (name: "sparse" | "dense", opts?: { renderer?: "webgl" | "canvas2d"; zoom?: number }) => BenchResult;
    __dprProbe: () => DprProbe;
    __pixelDiff: () => PixelDiff;
  }
}

window.__bench = bench;
window.__dprProbe = dprProbe;
window.__pixelDiff = pixelDiff;
