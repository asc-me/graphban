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

export interface ZoomRange {
  min?: number;
  max?: number;
}

/** A press under this many screen pixels of travel is a click, not a pan. */
export const PAN_SLOP_PX = 4;

export function clampZoom(k: number, min = MIN_ZOOM, max = MAX_ZOOM): number {
  return Math.max(min, Math.min(max, k));
}

/**
 * Zoom about a fixed point, so the thing under the cursor stays under the cursor.
 *
 * Zooming about the centre instead is the small wrongness that makes a graph feel unusable:
 * the user points at what they want, and the view walks away from it.
 */
export function zoomAbout(
  v: Viewport,
  factor: number,
  px: number,
  py: number,
  range?: ZoomRange,
): Viewport {
  const k = clampZoom(v.k * factor, range?.min, range?.max);
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
  range?: ZoomRange,
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
  const k = clampZoom(Math.min((width - padding * 2) / w, (height - padding * 2) / h), range?.min, range?.max);
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
    /** Swallows the click that ends a pan, so letting go over a box does not select it. */
    onClickCapture: (e: React.MouseEvent<SVGSVGElement>) => void;
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
export function useGraphViewport(
  width: number,
  height: number,
  opts?: {
    minZoom?: number;
    maxZoom?: number;
    /**
     * Pan from a press on anything in the scene, not only the bare `<svg>`. For a view whose
     * scene covers the canvas (Stack's ring frames are filled), where a background-only pan
     * never starts. The pan begins past PAN_SLOP_PX, so a click still reaches what it hit.
     */
    panAnywhere?: boolean;
  },
): GraphViewport {
  const [viewport, setViewport] = React.useState<Viewport>(IDENTITY);
  const [panning, setPanning] = React.useState(false);
  const svgRef = React.useRef<SVGSVGElement | null>(null);
  const panFrom = React.useRef<{
    x: number;
    y: number;
    vx: number;
    vy: number;
    cx: number;
    cy: number;
    started: boolean;
  } | null>(null);
  const swallowClick = React.useRef(false);
  const rangeRef = React.useRef<ZoomRange | undefined>(undefined);
  rangeRef.current = opts ? { min: opts.minZoom, max: opts.maxZoom } : undefined;
  const panAnywhere = opts?.panAnywhere ?? false;

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
      setViewport((v) => zoomAbout(v, factor, p.x, p.y, rangeRef.current));
    },
    [toView],
  );

  const onPointerDown = React.useCallback(
    (e: React.PointerEvent<SVGSVGElement>) => {
      swallowClick.current = false;
      if (panAnywhere) {
        // Primary button only: a right-click opens a menu, it does not grab the map.
        if (e.button !== 0) return;
      } else if (e.target !== e.currentTarget) {
        // Only the background pans; a node stops propagation and drags itself instead.
        return;
      }
      const p = toView(e.clientX, e.clientY);
      panFrom.current = {
        x: p.x,
        y: p.y,
        vx: viewport.x,
        vy: viewport.y,
        cx: e.clientX,
        cy: e.clientY,
        // A background-only pan starts at once; a pan-anywhere press waits for the slop.
        started: !panAnywhere,
      };
      if (!panAnywhere) {
        setPanning(true);
        e.currentTarget.setPointerCapture(e.pointerId);
      }
    },
    [toView, viewport, panAnywhere],
  );

  const onPointerMove = React.useCallback(
    (e: React.PointerEvent<SVGSVGElement>) => {
      const from = panFrom.current;
      if (!from) return;
      if (!from.started) {
        if (Math.hypot(e.clientX - from.cx, e.clientY - from.cy) < PAN_SLOP_PX) return;
        from.started = true;
        setPanning(true);
        e.currentTarget.setPointerCapture?.(e.pointerId);
      }
      const p = toView(e.clientX, e.clientY);
      setViewport((v) => ({ ...v, x: from.vx + (p.x - from.x), y: from.vy + (p.y - from.y) }));
    },
    [toView],
  );

  const onPointerUp = React.useCallback(
    (e: React.PointerEvent<SVGSVGElement>) => {
      // A pan-anywhere press that moved was a pan: the click it ends is not a selection.
      if (panAnywhere && panFrom.current?.started) swallowClick.current = true;
      panFrom.current = null;
      setPanning(false);
      if (e.currentTarget.hasPointerCapture?.(e.pointerId)) {
        e.currentTarget.releasePointerCapture(e.pointerId);
      }
    },
    [panAnywhere],
  );

  const onClickCapture = React.useCallback((e: React.MouseEvent<SVGSVGElement>) => {
    if (!swallowClick.current) return;
    swallowClick.current = false;
    e.stopPropagation();
    e.preventDefault();
  }, []);

  const reset = React.useCallback(() => setViewport(IDENTITY), []);
  const onDoubleClick = reset;

  const fitTo = React.useCallback(
    (points: Pos[]) => setViewport(fitViewport(points, width, height, 60, rangeRef.current)),
    [width, height],
  );

  return {
    viewport,
    transform: `translate(${viewport.x},${viewport.y}) scale(${viewport.k})`,
    panning,
    svgHandlers: { onWheel, onPointerDown, onPointerMove, onPointerUp, onClickCapture, onDoubleClick },
    fitTo,
    reset,
    setViewport,
    toWorld,
    svgRef,
  };
}
