/**
 * Canvas 2D renderer (PRD-52 D5). WebGL2 is a later slice behind `Renderer`.
 *
 * Fixed z-order: clouds, edges, nodes, rings. Labels and hull names stay in the DOM.
 */

import type { Renderer } from "./renderer";
import type { DrawScene } from "./scene";
import { drawClouds } from "./clouds";
import { applyCamera, drawNodes, drawRings } from "./nodes";

function drawGlow(ctx: CanvasRenderingContext2D, scene: DrawScene) {
  if (scene.glow.length === 0) return;
  const { camera } = scene;
  applyCamera(ctx, scene);
  ctx.shadowColor = "rgba(198,242,78,0.85)";
  // shadowBlur is in bitmap pixels and ignores the current transform.
  ctx.shadowBlur = 8 * scene.dpr;
  ctx.globalAlpha = 0.95;
  ctx.lineWidth = 2.5 / Math.max(camera.k, 0.05);
  for (const stroke of scene.glow) {
    ctx.beginPath();
    ctx.strokeStyle = stroke.color;
    ctx.moveTo(stroke.x0, stroke.y0);
    ctx.lineTo(stroke.x1, stroke.y1);
    ctx.stroke();
  }
  ctx.shadowBlur = 0;
  ctx.globalAlpha = 1;
}

function drawEdges(ctx: CanvasRenderingContext2D, scene: DrawScene) {
  const { camera } = scene;
  applyCamera(ctx, scene);
  for (const stroke of scene.strokes) {
    ctx.beginPath();
    ctx.strokeStyle = stroke.color;
    ctx.globalAlpha = 0.85;
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
      drawNodes(ctx, scene);
      drawRings(ctx, scene);
      drawGlow(ctx, scene);
    },
    destroy() {
      dead = true;
    },
  };
}
