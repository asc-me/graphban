/**
 * Node layer (PRD-52 D5). Kind is shape, area is colour.
 * Below 2.2px screen radius every node is a filled square.
 */

import { hexagonPoints, kindShape } from "../kindShape";
import type { DrawNode, DrawRing, DrawScene } from "./scene";

/**
 * World coordinates onto the bitmap.
 *
 * The camera maps world units to CSS pixels. The bitmap is `dpr` times the CSS
 * box, so the matrix has to carry both. A camera-only `setTransform` replaces
 * the dpr scale the clear just set, and on a 2× display the marks land in the
 * top-left quarter while the DOM hulls and labels stay in CSS pixels.
 */
export function applyCamera(ctx: CanvasRenderingContext2D, scene: DrawScene) {
  const { camera, dpr } = scene;
  const scale = dpr * camera.k;
  ctx.setTransform(scale, 0, 0, scale, dpr * camera.x, dpr * camera.y);
}

export function drawNodes(ctx: CanvasRenderingContext2D, scene: DrawScene): void {
  applyCamera(ctx, scene);
  for (const node of scene.nodes) drawOne(ctx, node, scene.camera.k);
}

function drawOne(ctx: CanvasRenderingContext2D, node: DrawNode, k: number) {
  ctx.save();
  ctx.globalAlpha = node.alpha;
  ctx.translate(node.x, node.y);
  ctx.fillStyle = node.color;
  ctx.strokeStyle = "#0a0c0e";
  ctx.lineWidth = 1.25 / Math.max(k, 0.05);
  const r = node.r;
  const shape = kindShape(node.kind, node.square);
  if (shape === "square") {
    ctx.fillRect(-r, -r, r * 2, r * 2);
  } else if (shape === "diamond") {
    ctx.beginPath();
    ctx.moveTo(0, -r);
    ctx.lineTo(r, 0);
    ctx.lineTo(0, r);
    ctx.lineTo(-r, 0);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
  } else if (shape === "hexagon") {
    const pts = hexagonPoints(r);
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
  } else if (shape === "ring") {
    ctx.beginPath();
    ctx.arc(0, 0, r, 0, Math.PI * 2);
    ctx.strokeStyle = node.color;
    ctx.lineWidth = 2 / Math.max(k, 0.05);
    ctx.stroke();
    ctx.beginPath();
    ctx.fillStyle = node.color;
    ctx.arc(0, 0, Math.max(2.2, r * 0.36), 0, Math.PI * 2);
    ctx.fill();
  } else {
    ctx.beginPath();
    ctx.arc(0, 0, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
  }
  ctx.restore();
}

/** Contention r+3, pin r+5, selection r+7. S2 draws selection; the others are the slots. */
export function drawRings(ctx: CanvasRenderingContext2D, scene: DrawScene): void {
  applyCamera(ctx, scene);
  const order: DrawRing["kind"][] = ["contention", "pin", "selection"];
  for (const kind of order) {
    for (const ring of scene.rings) {
      if (ring.kind !== kind) continue;
      ctx.beginPath();
      ctx.arc(ring.x, ring.y, ring.r, 0, Math.PI * 2);
      ctx.strokeStyle = ring.color;
      ctx.globalAlpha = ring.alpha ?? 0.9;
      ctx.lineWidth = 1.5 / Math.max(scene.camera.k, 0.05);
      ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;
}
