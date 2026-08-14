import * as React from "react";

import type { Pos } from "./layout";

export const MIN_ZOOM = 0.25;
export const MAX_ZOOM = 6;

/** Below this the graph is too small to read a label against, so D2 hides them. */
export const LABEL_ZOOM = 0.8;

export interface Viewport {
  /** Scale. */
  k: number;
  /** Translation, in viewBox units, applied before the scale. */
  x: number;
  y: number;
}

export const IDENTITY: Viewport = { k: 1, x: 0, y: 0 };

export function clampZoom(k: number): number {
  return Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, k));
}

/**
 * Zoom about a fixed point, so the thing under the cursor stays under the cursor.
 *
 * Zooming about the centre instead is the small wrongness that makes a graph feel unusable:
 * the user points at what they want, and the view walks away from it.
 */
export function zoomAbout(v: Viewport, factor: number, px: number, py: number): Viewport {
  const k = clampZoom(v.k * factor);
  if (k === v.k) return v;
  // The world point under (px, py) must be invariant: (p - t) / k is held constant.
  return { k, x: px - ((px - v.x) / v.k) * k, y: py - ((py - v.y) / v.k) * k };
}

/** Convert a client (screen) point to viewBox coordinates for an `<svg viewBox="0 0 w h">`. */
export function clientToView(
  el: SVGSVGElement,
  clientX: number,
  clientY: number,
  width: number,
  height: number,
): Pos {
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return { x: 0, y: 0 };
  return { x: ((clientX - r.left) / r.width) * width, y: ((clientY - r.top) / r.height) * height };
}

/** Viewport that frames `points` with padding, clamped to the zoom range. */
export function fitViewport(
  points: Pos[],
  width: number,
  height: number,
  padding = 60,
): Viewport {
  if (points.length === 0) return IDENTITY;
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const w = Math.max(1, maxX - minX);
  const h = Math.max(1, maxY - minY);
  const k = clampZoom(Math.min((width - padding * 2) / w, (height - padding * 2) / h));
  // Centre the box: the midpoint of the fitted content lands on the midpoint of the frame.
  return {
    k,
    x: width / 2 - ((minX + maxX) / 2) * k,
    y: height / 2 - ((minY + maxY) / 2) * k,
  };
}

export interface GraphViewport {
  viewport: Viewport;
  /** For the `<g>` that wraps the whole scene. */
  transform: string;
  /** True while the user is dragging the canvas — suppresses the ease so panning is direct. */
  panning: boolean;
  /** Handlers for the `<svg>`. Node drag is the view's business; this is background only. */
  svgHandlers: {
    onWheel: (e: React.WheelEvent<SVGSVGElement>) => void;
    onPointerDown: (e: React.PointerEvent<SVGSVGElement>) => void;
    onPointerMove: (e: React.PointerEvent<SVGSVGElement>) => void;
    onPointerUp: (e: React.PointerEvent<SVGSVGElement>) => void;
    onDoubleClick: () => void;
  };
  /** Ease the view to frame these points — used by find (D2) to fit the matches. */
  fitTo: (points: Pos[]) => void;
  reset: () => void;
  /** Exposed so keyboard panning (D3) moves the same state the pointer does. */
  setViewport: React.Dispatch<React.SetStateAction<Viewport>>;
  /** Screen -> world, for turning a pointer event into a node position while dragging. */
  toWorld: (clientX: number, clientY: number) => Pos;
  svgRef: React.RefObject<SVGSVGElement | null>;
}

/**
 * Pan and zoom for a fixed-viewBox SVG graph (PRD-20 D2).
 *
 * The views keep their `viewBox="0 0 W H"` — this transforms a `<g>` inside it rather than
 * rewriting the viewBox, so every coordinate in the scene stays in the same space the layout
 * produced and nothing downstream has to know the view moved.
 */
export function useGraphViewport(width: number, height: number): GraphViewport {
  const [viewport, setViewport] = React.useState<Viewport>(IDENTITY);
  const [panning, setPanning] = React.useState(false);
  const svgRef = React.useRef<SVGSVGElement | null>(null);
  const panFrom = React.useRef<{ x: number; y: number; vx: number; vy: number } | null>(null);

  const toView = React.useCallback(
    (clientX: number, clientY: number): Pos => {
      const el = svgRef.current;
      if (!el) return { x: 0, y: 0 };
      return clientToView(el, clientX, clientY, width, height);
    },
    [width, height],
  );

  const toWorld = React.useCallback(
    (clientX: number, clientY: number): Pos => {
      const p = toView(clientX, clientY);
      return { x: (p.x - viewport.x) / viewport.k, y: (p.y - viewport.y) / viewport.k };
    },
    [toView, viewport],
  );

  const onWheel = React.useCallback(
    (e: React.WheelEvent<SVGSVGElement>) => {
      const p = toView(e.clientX, e.clientY);
      // Exponential in the delta so a trackpad and a mouse wheel both feel proportional.
      const factor = Math.exp(-e.deltaY * 0.002);
      setViewport((v) => zoomAbout(v, factor, p.x, p.y));
    },
    [toView],
  );

  const onPointerDown = React.useCallback(
    (e: React.PointerEvent<SVGSVGElement>) => {
      // Only the background pans; a node stops propagation and drags itself instead.
      if (e.target !== e.currentTarget) return;
      const p = toView(e.clientX, e.clientY);
      panFrom.current = { x: p.x, y: p.y, vx: viewport.x, vy: viewport.y };
      setPanning(true);
      e.currentTarget.setPointerCapture(e.pointerId);
    },
    [toView, viewport],
  );

  const onPointerMove = React.useCallback(
    (e: React.PointerEvent<SVGSVGElement>) => {
      const from = panFrom.current;
      if (!from) return;
      const p = toView(e.clientX, e.clientY);
      setViewport((v) => ({ ...v, x: from.vx + (p.x - from.x), y: from.vy + (p.y - from.y) }));
    },
    [toView],
  );

  const onPointerUp = React.useCallback((e: React.PointerEvent<SVGSVGElement>) => {
    panFrom.current = null;
    setPanning(false);
    if (e.currentTarget.hasPointerCapture?.(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
  }, []);

  const reset = React.useCallback(() => setViewport(IDENTITY), []);
  const onDoubleClick = reset;

  const fitTo = React.useCallback(
    (points: Pos[]) => setViewport(fitViewport(points, width, height)),
    [width, height],
  );

  return {
    viewport,
    transform: `translate(${viewport.x},${viewport.y}) scale(${viewport.k})`,
    panning,
    svgHandlers: { onWheel, onPointerDown, onPointerMove, onPointerUp, onDoubleClick },
    fitTo,
    reset,
    setViewport,
    toWorld,
    svgRef,
  };
}
