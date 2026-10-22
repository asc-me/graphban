/**
 * Draws one fixture with the Canvas 2D renderer and reports paint, pan and
 * hit-test times. The page does not mount React: a pan frame only transforms
 * the cached layout.
 */

import { createCanvas2D } from "../../src/features/code/canvas/canvas2d";
import { buildScene } from "../../src/features/code/canvas/scene";
import { clustersLayout, hitTest } from "../../src/lib/graph/bigLayout";
import { dense, sparse, type BigMap } from "../../src/lib/graph/__fixtures__/bigMap";

export interface BenchResult {
  paintMs: number;
  panP95: number;
  hitMs: number;
  nodes: number;
}

function mapOf(name: "sparse" | "dense"): BigMap {
  return name === "sparse" ? sparse : dense;
}

function bench(name: "sparse" | "dense"): BenchResult {
  const map = mapOf(name);
  const nodes = map.nodes.map((n) => ({ id: n.id, path: n.path, kind: n.kind }));
  const edges = [...map.owns, ...map.edges];
  const canvas = document.getElementById("c") as HTMLCanvasElement;
  const renderer = createCanvas2D(canvas);
  if (!renderer) throw new Error("canvas 2d context missing");

  const t0 = performance.now();
  const layout = clustersLayout({ nodes, edges });
  const alpha = new Uint8Array(layout.ids.length);
  alpha.fill(255);
  const base = {
    width: 1200,
    height: 800,
    dpr: 1,
    alpha,
    types: null,
    isolate: null,
    selectedId: null,
    hoverId: null,
  };
  renderer.draw(buildScene(layout, { ...base, camera: { x: 40, y: 30, k: 1 } }));
  const paintMs = performance.now() - t0;

  const samples: number[] = [];
  let cam = { x: 40, y: 30, k: 1 };
  for (let i = 0; i < 40; i++) {
    cam = { x: cam.x + 4, y: cam.y + 2, k: cam.k };
    const t = performance.now();
    renderer.draw(buildScene(layout, { ...base, camera: cam }));
    samples.push(performance.now() - t);
  }
  samples.sort((a, b) => a - b);
  const panP95 = samples[Math.min(samples.length - 1, Math.floor(samples.length * 0.95))];

  const ht0 = performance.now();
  const probes = 50;
  for (let i = 0; i < probes; i++) hitTest(layout, 80 + i * 3, 60 + i);
  const hitMs = (performance.now() - ht0) / probes;
  renderer.destroy();
  return { paintMs, panP95, hitMs, nodes: layout.nodeCount };
}

declare global {
  interface Window {
    __bench: (name: "sparse" | "dense") => BenchResult;
  }
}

window.__bench = bench;
