/**
 * Canvas camera (PRD-52 D5).
 *
 * Wheel and drag are live. Programmatic moves tween for 600ms, ease-out-cubic,
 * and a new target replaces the one in flight. Reduced motion jumps.
 * Scale is 0.7×–16×, which is not the SVG viewport's range. The floor drops below 0.7
 * only when a whole layout needs it to fit (`zoomFloor`).
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

/** The scale that frames `points` with `pad`, unclamped. Null when there is nothing to frame. */
export function fitScale(
  points: { x: number; y: number; r?: number }[],
  width: number,
  height: number,
  pad = 48,
): number | null {
  if (points.length === 0 || width <= 0 || height <= 0) return null;
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
  return Math.min((width - pad * 2) / bw, (height - pad * 2) / bh);
}

/**
 * The lowest scale the camera allows for this layout: 0.7, or lower when the whole map
 * needs it. A Map treemap can be taller than the frame at 0.7, and a fixed floor left Fit
 * and zoom-out unable to show all of it.
 */
export function zoomFloor(fit: number | null): number {
  if (fit === null || !Number.isFinite(fit)) return CANVAS_MIN_K;
  return Math.max(0.05, Math.min(CANVAS_MIN_K, fit));
}

export function fitCamera(
  points: { x: number; y: number; r?: number }[],
  width: number,
  height: number,
  pad = 48,
  minK = CANVAS_MIN_K,
): Cam {
  const raw = fitScale(points, width, height, pad);
  if (raw === null || !Number.isFinite(raw)) return { x: 0, y: 0, k: 1 };
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
  const k = Math.max(minK, Math.min(CANVAS_MAX_K, raw));
  return {
    k,
    x: width / 2 - ((minX + maxX) / 2) * k,
    y: height / 2 - ((minY + maxY) / 2) * k,
  };
}

/**
 * Frame a pair of hulls, then raise the scale to at least `minK` around that
 * same centre. Fitting alone can sit below the fine-edge zoom; the pair is
 * still the target, so the centre does not jump to one hull.
 */
export function framePair(
  points: { x: number; y: number; r?: number }[],
  width: number,
  height: number,
  minK: number,
): Cam {
  const fit = fitCamera(points, width, height);
  if (fit.k >= minK || fit.k <= 0) return fit;
  const midX = (width / 2 - fit.x) / fit.k;
  const midY = (height / 2 - fit.y) / fit.k;
  return framePoint(midX, midY, minK, width, height);
}

export function prefersReducedMotion(): boolean {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}
