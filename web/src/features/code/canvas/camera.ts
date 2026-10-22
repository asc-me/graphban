/**
 * Canvas camera (PRD-52 D5).
 *
 * Wheel and drag are live. Programmatic moves tween for 600ms, ease-out-cubic,
 * and a new target replaces the one in flight. Reduced motion jumps.
 * Scale is 0.7×–16×, which is not the SVG viewport's range.
 */

export const CANVAS_MIN_K = 0.7;
export const CANVAS_MAX_K = 16;
export const TWEEN_MS = 600;
export const FOCUS_ZOOM = 7;

export interface Cam {
  x: number;
  y: number;
  k: number;
}

export interface Tween {
  from: Cam;
  to: Cam;
  start: number;
}

export function easeOutCubic(t: number): number {
  const u = 1 - Math.min(1, Math.max(0, t));
  return 1 - u * u * u;
}

export function clampCanvasZoom(k: number): number {
  return Math.max(CANVAS_MIN_K, Math.min(CANVAS_MAX_K, k));
}

/** A new target replaces any running tween. Nothing is queued behind it. */
export function replaceTween(
  from: Cam,
  to: Cam,
  now: number,
  reduced: boolean,
): { cam: Cam; tween: Tween | null } {
  if (reduced) return { cam: to, tween: null };
  return { cam: from, tween: { from, to, start: now } };
}

export function sampleTween(tween: Tween, now: number): { cam: Cam; done: boolean } {
  const t = Math.min(1, Math.max(0, (now - tween.start) / TWEEN_MS));
  const e = easeOutCubic(t);
  return {
    cam: {
      x: tween.from.x + (tween.to.x - tween.from.x) * e,
      y: tween.from.y + (tween.to.y - tween.from.y) * e,
      k: tween.from.k + (tween.to.k - tween.from.k) * e,
    },
    done: t >= 1,
  };
}

export function framePoint(x: number, y: number, k: number, width: number, height: number): Cam {
  const kk = clampCanvasZoom(k);
  return { k: kk, x: width / 2 - x * kk, y: height / 2 - y * kk };
}

export function fitCamera(
  points: { x: number; y: number; r?: number }[],
  width: number,
  height: number,
  pad = 48,
): Cam {
  if (points.length === 0 || width <= 0 || height <= 0) return { x: 0, y: 0, k: 1 };
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const p of points) {
    const r = p.r ?? 0;
    minX = Math.min(minX, p.x - r);
    maxX = Math.max(maxX, p.x + r);
    minY = Math.min(minY, p.y - r);
    maxY = Math.max(maxY, p.y + r);
  }
  const bw = Math.max(1, maxX - minX);
  const bh = Math.max(1, maxY - minY);
  const k = clampCanvasZoom(Math.min((width - pad * 2) / bw, (height - pad * 2) / bh));
  return {
    k,
    x: width / 2 - ((minX + maxX) / 2) * k,
    y: height / 2 - ((minY + maxY) / 2) * k,
  };
}

export function prefersReducedMotion(): boolean {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}
