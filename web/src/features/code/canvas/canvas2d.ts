/**
 * Canvas 2D renderer (PRD-52 D5). Fallback when WebGL2 is missing or lost.
 *
 * Fixed z-order: clouds, edges, glow, nodes, rings. Labels and hull names stay in the DOM.
 */

import type { Renderer } from "./renderer";
import { GLOW_CORE_ALPHA, GLOW_CORE_PX, GLOW_WASH_ALPHA, GLOW_WASH_PX, type DrawScene } from "./scene";
import { drawClouds } from "./clouds";
import { applyCamera, drawNodes, drawRings } from "./nodes";

function drawGlow(ctx: CanvasRenderingContext2D, scene: DrawScene) {
  if (scene.glow.length === 0) return;
  const { camera } = scene;
  const fade = scene.focusMix ?? 1;
  const k = Math.max(camera.k, 0.05);
  applyCamera(ctx, scene);
  ctx.lineCap = "round";
  // Build.dc.html: a 5px wash at 0.18 under a 1.3px core at 0.95, in the edge colour.
  for (const [width, alpha] of [
    [GLOW_WASH_PX, GLOW_WASH_ALPHA],
    [GLOW_CORE_PX, GLOW_CORE_ALPHA],
  ] as const) {
    ctx.globalAlpha = alpha * fade;
    ctx.lineWidth = width / k;
    for (const stroke of scene.glow) {
      ctx.beginPath();
      ctx.strokeStyle = stroke.color;
      ctx.moveTo(stroke.x0, stroke.y0);
      if (stroke.kind === "quad") ctx.quadraticCurveTo(stroke.cx, stroke.cy, stroke.x1, stroke.y1);
      else ctx.lineTo(stroke.x1, stroke.y1);
      ctx.stroke();
    }
  }
  ctx.lineCap = "butt";
  ctx.globalAlpha = 1;
}

function drawEdges(ctx: CanvasRenderingContext2D, scene: DrawScene) {
  const { camera } = scene;
  applyCamera(ctx, scene);
  // One alpha for the batch. A path dims every non-path edge by setting this
  // once; deciding inside the loop would walk every edge on the path frame.
  ctx.globalAlpha = scene.edgesAlpha;
  for (const stroke of scene.strokes) {
    ctx.beginPath();
    ctx.strokeStyle = stroke.color;
    ctx.lineWidth = stroke.widthPx / Math.max(camera.k, 0.05);
    if (stroke.kind === "quad") {
      ctx.moveTo(stroke.x0, stroke.y0);
      ctx.quadraticCurveTo(stroke.cx, stroke.cy, stroke.x1, stroke.y1);
    } else {
      ctx.moveTo(stroke.x0, stroke.y0);
      ctx.lineTo(stroke.x1, stroke.y1);
    }
    ctx.stroke();
    if (stroke.marker === "double") {
      const mx = 0.25 * stroke.x0 + 0.5 * stroke.cx + 0.25 * stroke.x1;
      const my = 0.25 * stroke.y0 + 0.5 * stroke.cy + 0.25 * stroke.y1;
      ctx.beginPath();
      ctx.arc(mx, my, 3 / Math.max(camera.k, 0.05), 0, Math.PI * 2);
      ctx.fillStyle = stroke.color;
      ctx.fill();
    }
  }
  ctx.globalAlpha = 1;
}

export function createCanvas2D(canvas: HTMLCanvasElement): Renderer | null {
  let ctx: CanvasRenderingContext2D | null;
  try {
    ctx = canvas.getContext("2d");
  } catch {
    // jsdom throws here. A missing context is the reduced view, not a crash.
    return null;
  }
  if (!ctx) return null;
  let dead = false;
  return {
    kind: "canvas2d",
    draw(scene: DrawScene) {
      if (dead) return;
      const dpr = scene.dpr;
      const pxW = Math.max(1, Math.round(scene.width * dpr));
      const pxH = Math.max(1, Math.round(scene.height * dpr));
      if (canvas.width !== pxW) canvas.width = pxW;
      if (canvas.height !== pxH) canvas.height = pxH;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, scene.width, scene.height);
      drawClouds(ctx, scene);
      drawEdges(ctx, scene);
      // Under the nodes, as Build.dc.html orders gGlow before gNodes.
      drawGlow(ctx, scene);
      drawNodes(ctx, scene);
      drawRings(ctx, scene);
    },
    destroy() {
      dead = true;
    },
  };
}
