/**
 * Canvas mode for a code map above DETAIL_BUDGET (PRD-52 D1, D4, D5).
 *
 * Clusters, Map and Orbit are drawings. Matrix is the DOM grid (PRD-52 D4)
 * and replaces this surface when that chip is on. The drawing is Canvas 2D
 * behind the Renderer interface; labels, hull names and the tooltip are DOM.
 */

import * as React from "react";

import { zoomAbout } from "@/lib/graph/useGraphViewport";
import { pathAlphaBuffer } from "@/lib/graph/pathHighlight";
import { hitTest, type AreaLayoutKind, type ClusterLayout } from "@/lib/graph/bigLayout";
import { useAreaLayout } from "@/lib/graph/useAreaLayout";
import { AreaMatrix, LayoutChips, type GraphLayoutId } from "./AreaMatrix";
import type { CodePath } from "@/lib/types";
import { createCanvas2D } from "./canvas/canvas2d";
import {
  CANVAS_MAX_K,
  CANVAS_MIN_K,
  FOCUS_ZOOM,
  fitCamera,
  framePair,
  framePoint,
  prefersReducedMotion,
  replaceTween,
  sampleTween,
  type Cam,
  type Tween,
} from "./canvas/camera";
import { LOD_ZOOM, groupThousands, hitBundle } from "./canvas/edges";
import { pickLabels, type LabelNode } from "./canvas/labels";
import type { Renderer } from "./canvas/renderer";
import {
  NON_PATH_EDGE_ALPHA,
  PathBanner,
  pathSegments,
} from "./canvas/overlay";
import {
  buildScene,
  reachOf,
  worldInView,
  writeAlpha,
  type DrawScene,
} from "./canvas/scene";

export interface CanvasPathState {
  /** Armed: crosshair, banner, clicks pick a target instead of a selection. */
  picking: boolean;
  pending: boolean;
  slow: boolean;
  /** Set only when a route with hops should dim the canvas and draw on the overlay. */
  path: CodePath | null;
}

export interface CanvasGraphNode {
  id: string;
  path: string;
  kind: string;
  name: string;
}

export interface CanvasGraphEdge {
  src: string;
  dst: string;
  type: string;
}

function shortName(node: { path: string; name: string } | undefined, id: string): string {
  if (node?.name) return node.name;
  const seg = (node?.path ?? id).split("::").pop() ?? id;
  const parts = seg.split("/").filter(Boolean);
  return parts.pop() ?? seg;
}

/**
 * A drawn file under the pointer. Above 10k nodes the frame is areas and
 * bundles until one area fills the view, and a hit on a file that is not
 * drawn selects something the picture does not contain.
 *
 * Opening that area does not hide the others: the reveal test is a 35%
 * screen radius, so neighbouring hulls stay on screen. Click, hover and
 * double-click all come through here, and a pointer on one of those hulls
 * is not a file in the area this frame drew.
 */
function nodeAtPointer(
  scene: DrawScene | null,
  layout: ClusterLayout | null,
  wx: number,
  wy: number,
): string | null {
  if (!scene || !layout || scene.areasOnly) return null;
  const id = hitTest(layout, wx, wy);
  if (!id || !scene.revealed) return id;
  const i = layout.ids.indexOf(id);
  if (i < 0 || layout.areaOf[i] !== scene.revealed) return null;
  return id;
}

/** The Map tile under a world point, or null. Node hits win over this. */
function tileAt(layout: ClusterLayout | null, wx: number, wy: number): string | null {
  if (!layout || (layout.layout ?? "clusters") !== "map") return null;
  for (const area of layout.areas) {
    const rect = area.rect;
    if (!rect) continue;
    if (wx >= rect.x && wx < rect.x + rect.w && wy >= rect.y && wy < rect.y + rect.h) return area.id;
  }
  return null;
}

export interface CanvasControlApi {
  zoomBy: (factor: number) => void;
  fit: () => void;
}

export function CodeGraphCanvas({
  nodes,
  edges,
  enabledTypes,
  isolate,
  selectedId,
  onSelect,
  truncated,
  totalNodes,
  frameId,
  controlRef,
  onZoom,
  layout: layoutProp,
  onLayout,
  pairFocus = null,
  returnTo = null,
  frameAreaId = null,
  frameAreaNonce = 0,
  prevDepth,
  onDepth,
  pathState,
  onPathTarget,
  onPathEmpty,
}: {
  nodes: CanvasGraphNode[];
  edges: CanvasGraphEdge[];
  enabledTypes: string[];
  isolate: boolean;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  /** True when the payload says the map is a page, or the flag is absent and the counts disagree. */
  truncated: boolean;
  totalNodes: number;
  /** An external request (find, hubs) to tween this node to 7×. */
  frameId: string | null;
  /** The view's GraphControls cluster drives zoom and fit through this. */
  controlRef?: { current: CanvasControlApi | null };
  /** Fired when the camera scale changes, so the shared readout can follow. */
  onZoom?: (k: number) => void;
  /** Controlled by the view. Omitted, the chip row keeps its own choice. */
  layout?: GraphLayoutId;
  onLayout?: (id: GraphLayoutId) => void;
  /** Show in graph: only this directed area pair, at fine zoom. */
  pairFocus?: { src: string; dst: string } | null;
  returnTo?: { text: string; onClick: () => void } | null;
  /** Explore: frame this area's hull. `nonce` retriggers the same id. */
  frameAreaId?: string | null;
  frameAreaNonce?: number;
  /** Depth kept by the view across a Matrix unmount. Seed for the next layout. */
  prevDepth?: number;
  /** Reports the depth this layout actually used, so the grid can keep it. */
  onDepth?: (depth: number) => void;
  /** Path mode. Absent on the SVG-sized maps this canvas is not showing. */
  pathState?: CanvasPathState | null;
  /** A node click while armed or pending. Does not change the selection. */
  onPathTarget?: (id: string) => void;
  /** Empty canvas while armed or pending. Disarms; does not clear the selection. */
  onPathEmpty?: () => void;
}) {
  const hostRef = React.useRef<HTMLDivElement | null>(null);
  const canvasRef = React.useRef<HTMLCanvasElement | null>(null);
  const rendererRef = React.useRef<Renderer | null>(null);
  const [size, setSize] = React.useState({ w: 900, h: 560 });
  const [cam, setCam] = React.useState<Cam>({ x: 0, y: 0, k: 1 });
  const [tween, setTween] = React.useState<Tween | null>(null);
  const [hover, setHover] = React.useState<string | null>(null);
  const [hoverTile, setHoverTile] = React.useState<string | null>(null);
  const [selectedTile, setSelectedTile] = React.useState<string | null>(null);
  const [tip, setTip] = React.useState<{ x: number; y: number; text: string } | null>(null);
  const [cameraOwner, setCameraOwner] = React.useState<"fit" | "pair" | "area" | "node">("fit");
  const camRef = React.useRef(cam);
  camRef.current = cam;
  const sizeRef = React.useRef(size);
  sizeRef.current = size;
  const panRef = React.useRef<{ px: number; py: number; cx: number; cy: number } | null>(null);
  const fitted = React.useRef("");
  const [localLayout, setLocalLayout] = React.useState<GraphLayoutId>("clusters");
  const layoutMode: GraphLayoutId = onLayout ? (layoutProp ?? "clusters") : localLayout;
  const layoutKind: AreaLayoutKind = layoutMode === "matrix" ? "clusters" : layoutMode;
  const chooseLayout = React.useCallback(
    (id: GraphLayoutId) => {
      if (onLayout) onLayout(id);
      else {
        setLocalLayout(id);
        if (id !== "matrix") {
          setHover(null);
          setHoverTile(null);
          setSelectedTile(null);
          setTween(null);
          fitted.current = "";
        }
      }
    },
    [onLayout],
  );

  const input = React.useMemo(
    () =>
      nodes.length > 0
        ? {
            nodes: nodes.map((n) => ({ id: n.id, path: n.path, kind: n.kind })),
            edges,
            layout: layoutKind,
            prevDepth,
          }
        : null,
    [nodes, edges, layoutKind, prevDepth],
  );
  const area = useAreaLayout(input);
  const layout = area.layout;
  const layoutRef = React.useRef(layout);
  layoutRef.current = layout;

  React.useEffect(() => {
    onZoom?.(cam.k);
  }, [cam.k, onZoom]);

  React.useEffect(() => {
    if (!controlRef) return;
    controlRef.current = {
      zoomBy(factor) {
        const { w, h } = sizeRef.current;
        setTween(null);
        setCam((c) =>
          zoomAbout(c, factor, w / 2, h / 2, { min: CANVAS_MIN_K, max: CANVAS_MAX_K }),
        );
      },
      fit() {
        const lay = layoutRef.current;
        const { w, h } = sizeRef.current;
        if (!lay) return;
        setTween(null);
        setCam(fitCamera(lay.areas.map((a) => ({ x: a.cx, y: a.cy, r: a.r })), w, h));
      },
    };
    return () => {
      controlRef.current = null;
    };
  }, [controlRef]);

  React.useEffect(() => {
    const el = hostRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      if (r.width >= 2 && r.height >= 2) setSize({ w: r.width, h: r.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const sig = layout
    ? `${layout.layout ?? "clusters"}:${layout.nodeCount}:${layout.depth ?? "x"}:${layout.ids[0] ?? ""}`
    : "";
  const kindRef = React.useRef(layout?.layout ?? "clusters");
  React.useEffect(() => {
    if (!layout || !sig) return;
    const kind = layout.layout ?? "clusters";
    const kindChanged = kindRef.current !== kind;
    kindRef.current = kind;
    if (!kindChanged && fitted.current === sig) return;
    // A pair, an area zoom, or a node frame owns the camera. Marking the sig
    // keeps the fit-all from running the moment that request clears. A
    // drawing-kind change still fits: switching layout resets the camera.
    if (
      !kindChanged &&
      (pairFocus || frameAreaId || (frameId && layout.ids.includes(frameId)))
    ) {
      fitted.current = sig;
      return;
    }
    fitted.current = sig;
    setTween(null);
    setCam(fitCamera(layout.areas.map((a) => ({ x: a.cx, y: a.cy, r: a.r })), size.w, size.h));
    setCameraOwner("fit");
  }, [layout, sig, size.w, size.h, frameId, pairFocus, frameAreaId]);

  React.useEffect(() => {
    if (!pairFocus || !layout) return;
    const hulls = layout.areas.filter((a) => a.id === pairFocus.src || a.id === pairFocus.dst);
    if (hulls.length === 0) return;
    const to = framePair(
      hulls.map((h) => ({ x: h.cx, y: h.cy, r: h.r })),
      size.w,
      size.h,
      LOD_ZOOM,
    );
    const next = replaceTween(camRef.current, to, performance.now(), prefersReducedMotion());
    setCam(next.cam);
    setTween(next.tween);
    setCameraOwner("pair");
  }, [pairFocus, layout, size.w, size.h]);

  React.useEffect(() => {
    if (!frameAreaId || !layout || pairFocus) return;
    const hull = layout.areas.find((a) => a.id === frameAreaId);
    if (!hull) return;
    const to = fitCamera([{ x: hull.cx, y: hull.cy, r: hull.r }], size.w, size.h);
    const next = replaceTween(camRef.current, to, performance.now(), prefersReducedMotion());
    setCam(next.cam);
    setTween(next.tween);
    setCameraOwner("area");
  }, [frameAreaId, frameAreaNonce, layout, size.w, size.h, pairFocus]);

  React.useEffect(() => {
    // A find or a hub pick frames one node at 7×. That loses to a pair and to
    // an area zoom: otherwise this effect runs again on every layout arrival
    // and the camera settles on the node instead of both hulls.
    if (pairFocus || frameAreaId || !frameId || !layout) return;
    const i = layout.ids.indexOf(frameId);
    if (i < 0) return;
    const to = framePoint(layout.x[i], layout.y[i], FOCUS_ZOOM, size.w, size.h);
    const next = replaceTween(camRef.current, to, performance.now(), prefersReducedMotion());
    setCam(next.cam);
    setTween(next.tween);
    setCameraOwner("node");
  }, [frameId, layout, size.w, size.h, pairFocus, frameAreaId]);

  React.useEffect(() => {
    if (layout?.depth == null) return;
    onDepth?.(layout.depth);
  }, [layout, onDepth]);

  React.useEffect(() => {
    if (!tween) return;
    let raf = 0;
    const step = (now: number) => {
      const sample = sampleTween(tween, now);
      setCam(sample.cam);
      if (!sample.done) raf = requestAnimationFrame(step);
      else setTween(null);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [tween]);

  const nameOf = React.useMemo(() => {
    const m = new Map<string, CanvasGraphNode>();
    for (const n of nodes) m.set(n.id, n);
    return m;
  }, [nodes]);

  const types = React.useMemo(() => new Set(enabledTypes), [enabledTypes]);
  const degree = React.useMemo(() => {
    const d = new Map<string, number>();
    if (!layout) return d;
    for (const id of layout.ids) d.set(id, 0);
    for (const e of layout.edges) {
      if (!types.has(e.type)) continue;
      d.set(e.src, (d.get(e.src) ?? 0) + 1);
      d.set(e.dst, (d.get(e.dst) ?? 0) + 1);
    }
    return d;
  }, [layout, types]);

  const focusId = pairFocus ? null : hover ?? selectedId;
  const pairReach = React.useMemo(() => {
    if (!pairFocus || !layout) return null;
    const out = new Uint8Array(layout.ids.length);
    let any = false;
    for (let i = 0; i < layout.ids.length; i++) {
      const areaId = layout.areaOf[i];
      if (areaId === pairFocus.src || areaId === pairFocus.dst) {
        out[i] = 1;
        any = true;
      }
    }
    return any ? out : null;
  }, [pairFocus, layout]);
  const reach = React.useMemo(
    () =>
      pairReach ??
      (layout ? reachOf(layout, focusId, layout.edges.filter((e) => types.has(e.type))) : null),
    [pairReach, layout, focusId, types],
  );
  const drawnLayout = React.useMemo(() => {
    if (!layout) return null;
    if (!pairFocus) return layout;
    const edges = layout.edges.filter(
      (e) => types.has(e.type) && e.srcArea === pairFocus.src && e.dstArea === pairFocus.dst,
    );
    return { ...layout, edges };
  }, [layout, pairFocus, types]);
  // The path buffer replaces the reach buffer. It is keyed on the path, not the
  // camera, so a pan does not write it again.
  const pathAlpha = React.useMemo(
    () => (layout && pathState?.path ? pathAlphaBuffer(layout.ids, pathState.path) : null),
    [layout, pathState?.path],
  );
  const alpha = React.useMemo(() => {
    if (!layout) return null;
    if (pathAlpha) return pathAlpha;
    return writeAlpha(layout.ids.length, reach, isolate && !!reach);
  }, [layout, pathAlpha, reach, isolate]);
  const segments = React.useMemo(
    () => (layout && pathState?.path ? pathSegments(pathState.path, layout.ids, layout.x, layout.y) : []),
    [layout, pathState?.path],
  );
  const tracing = !!(pathState?.picking || pathState?.pending);
  const isolateSet = React.useMemo(() => {
    if (!isolate || !reach || !layout) return null;
    const set = new Set<string>();
    for (let i = 0; i < layout.ids.length; i++) if (reach[i]) set.add(layout.ids[i]);
    return set;
  }, [isolate, reach, layout]);

  const dpr = typeof window === "undefined" ? 1 : Math.min(2, window.devicePixelRatio || 1);
  const scene = React.useMemo(() => {
    if (!drawnLayout || !alpha) return null;
    return buildScene(drawnLayout, {
      camera: cam,
      width: size.w,
      height: size.h,
      dpr,
      alpha,
      types,
      // A path dims the whole edge batch. Isolate would drop those edges instead,
      // which is a different picture from "everything off the path, still drawn".
      isolate: pathAlpha ? null : isolateSet,
      selectedId,
      // Hover stays in the tip. Feeding it to the scene would glow non-path edges
      // back up to full strength over the dim.
      hoverId: pathAlpha ? null : hover,
      edgesAlpha: pathAlpha ? NON_PATH_EDGE_ALPHA : undefined,
      tileId: hoverTile ?? selectedTile,
    });
  }, [drawnLayout, alpha, pathAlpha, cam, size.w, size.h, dpr, types, isolateSet, selectedId, hover, hoverTile, selectedTile]);

  React.useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !scene) return;
    if (!rendererRef.current) rendererRef.current = createCanvas2D(canvas);
    rendererRef.current?.draw(scene);
  }, [scene]);

  React.useEffect(() => () => rendererRef.current?.destroy(), []);

  const toWorld = React.useCallback((clientX: number, clientY: number) => {
    const el = hostRef.current;
    const c = camRef.current;
    if (!el) return { x: 0, y: 0, sx: 0, sy: 0 };
    const r = el.getBoundingClientRect();
    const sx = r.width ? ((clientX - r.left) / r.width) * size.w : 0;
    const sy = r.height ? ((clientY - r.top) / r.height) * size.h : 0;
    return { x: (sx - c.x) / c.k, y: (sy - c.y) / c.k, sx, sy };
  }, [size.w, size.h]);

  const onWheel = (e: React.WheelEvent) => {
    const el = hostRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const px = r.width ? ((e.clientX - r.left) / r.width) * size.w : size.w / 2;
    const py = r.height ? ((e.clientY - r.top) / r.height) * size.h : size.h / 2;
    setTween(null);
    setCam((c) => zoomAbout(c, Math.exp(-e.deltaY * 0.0015), px, py, { min: CANVAS_MIN_K, max: CANVAS_MAX_K }));
  };

  const move = (clientX: number, clientY: number, sceneNow: DrawScene | null, layoutNow: ClusterLayout | null) => {
    const w = toWorld(clientX, clientY);
    if (panRef.current) {
      const from = panRef.current;
      setCam((c) => ({ ...c, x: from.cx + (w.sx - from.px), y: from.cy + (w.sy - from.py) }));
      return;
    }
    if (!layoutNow || !sceneNow) {
      setHover(null);
      setTip(null);
      return;
    }
    const hit = nodeAtPointer(sceneNow, layoutNow, w.x, w.y);
    if (hit) {
      setHover(hit);
      setHoverTile(null);
      const node = nameOf.get(hit);
      setTip({ x: w.sx + 12, y: w.sy - 8, text: shortName(node, hit) });
      return;
    }
    setHover(null);
    const tile = tileAt(layoutNow, w.x, w.y);
    setHoverTile(tile);
    if (tile) {
      const area = layoutNow.areas.find((a) => a.id === tile);
      setTip({ x: w.sx + 12, y: w.sy - 8, text: area?.label ?? tile });
      return;
    }
    const bundle = hitBundle(sceneNow.strokes, w.x, w.y, camRef.current);
    setTip(bundle ? { x: w.sx + 12, y: w.sy - 8, text: bundle.label } : null);
  };

  const labels = React.useMemo(() => {
    if (!layout || !scene) return [];
    const focus = hover ?? selectedId;
    const fi = focus ? layout.ids.indexOf(focus) : -1;
    const neighbors: string[] = [];
    if (focus) {
      for (const e of layout.edges) {
        if (!types.has(e.type)) continue;
        if (e.src === focus) neighbors.push(e.dst);
        else if (e.dst === focus) neighbors.push(e.src);
      }
    }
    const labelNodes: LabelNode[] = layout.ids.map((id, i) => {
      const text = shortName(nameOf.get(id), id);
      const sx = layout.x[i] * cam.k + cam.x;
      const sy = layout.y[i] * cam.k + cam.y;
      return {
        id,
        area: layout.areaOf[i],
        degree: degree.get(id) ?? 0,
        inView: scene.nodes.some((n) => n.id === id) || id === selectedId || id === hover,
        box: { x: sx + 8, y: sy - 8, w: Math.min(180, 8 + text.length * 7), h: 14 },
      };
    });
    return pickLabels({
      nodes: labelNodes,
      selectedId,
      hoverId: hover,
      neighbors,
      focusKind: fi >= 0 ? layout.kinds[fi] : "file",
      zoom: cam.k,
    });
  }, [layout, scene, hover, selectedId, cam.k, cam.x, cam.y, degree, nameOf, types]);

  const shown = nodes.length;
  const pairEdges = pairFocus && drawnLayout ? drawnLayout.edges.length : 0;

  if (layoutMode === "matrix") {
    return (
      <AreaMatrix
        nodes={nodes}
        edges={edges}
        enabledTypes={enabledTypes}
        selected={null}
        onSelect={() => {}}
        onLayout={chooseLayout}
        prevDepth={prevDepth}
      />
    );
  }

  return (
    <div
      ref={hostRef}
      data-testid="code-graph-canvas"
      data-renderer={rendererRef.current?.kind ?? "canvas2d"}
      data-pair={pairFocus ? `${pairFocus.src}→${pairFocus.dst}` : undefined}
      data-pair-edges={pairFocus ? String(pairEdges) : undefined}
      data-frame-area={frameAreaId ?? undefined}
      data-frame={cameraOwner}
      data-depth={layout?.depth ?? ""}
      data-camera-x={cam.x}
      role="application"
      aria-label={`Code graph canvas: ${shown} nodes. ${layoutKind[0].toUpperCase()}${layoutKind.slice(1)} layout.`}
      className={`absolute inset-0 touch-none${tracing ? " cursor-crosshair" : ""}`}
      onWheel={onWheel}
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        const target = e.target as HTMLElement;
        if (target.closest("button")) return;
        const w = toWorld(e.clientX, e.clientY);
        panRef.current = { px: w.sx, py: w.sy, cx: cam.x, cy: cam.y };
        setTween(null);
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => move(e.clientX, e.clientY, scene, layout)}
      onPointerUp={(e) => {
        const from = panRef.current;
        panRef.current = null;
        if (from) {
          const w = toWorld(e.clientX, e.clientY);
          const moved = Math.hypot(w.sx - from.px, w.sy - from.py);
          if (moved < 4 && layout) {
            const hit = nodeAtPointer(scene, layout, w.x, w.y);
            // Armed and pending both take a target. An empty click disarms
            // without clearing the selection the path starts from.
            if (tracing) {
              if (hit) onPathTarget?.(hit);
              else onPathEmpty?.();
              return;
            }
            if (hit) {
              setSelectedTile(null);
              onSelect(hit);
            } else {
              const tile = tileAt(layout, w.x, w.y);
              setSelectedTile(tile);
              onSelect(null);
            }
          }
        }
      }}
      onDoubleClick={(e) => {
        if (tracing) return;
        if (!layout) return;
        const w = toWorld(e.clientX, e.clientY);
        const hit = nodeAtPointer(scene, layout, w.x, w.y);
        if (!hit) {
          setTween(null);
          setCam(fitCamera(layout.areas.map((a) => ({ x: a.cx, y: a.cy, r: a.r })), size.w, size.h));
          return;
        }
        const i = layout.ids.indexOf(hit);
        const to = framePoint(layout.x[i], layout.y[i], FOCUS_ZOOM, size.w, size.h);
        const next = replaceTween(camRef.current, to, performance.now(), prefersReducedMotion());
        setCam(next.cam);
        setTween(next.tween);
        onSelect(hit);
      }}
    >
      <canvas ref={canvasRef} className="h-full w-full" />
      {scene && (
        <svg className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden>
          {scene.hulls.map((h) => {
            const sx = h.cx * cam.k + cam.x;
            const sy = h.cy * cam.k + cam.y;
            const sr = h.r * cam.k;
            const name = (
              <>
                {h.label}
                {h.showCount ? ` · ${h.count}` : ""}
              </>
            );
            if (h.rect) {
              const rx = h.rect.x * cam.k + cam.x;
              const ry = h.rect.y * cam.k + cam.y;
              const rw = h.rect.w * cam.k;
              const rh = h.rect.h * cam.k;
              return (
                <g key={h.id} data-testid={`hull-${h.id}`}>
                  <rect x={rx} y={ry} width={rw} height={rh} fill={h.color} fillOpacity={0.05} stroke={h.color} strokeOpacity={0.35} />
                  {h.showName && (
                    <text x={rx + rw / 2} y={ry + 14} textAnchor="middle" fill={h.color} fontSize={11}>
                      {name}
                    </text>
                  )}
                </g>
              );
            }
            return (
              <g key={h.id} data-testid={`hull-${h.id}`}>
                <circle cx={sx} cy={sy} r={sr} fill={h.color} fillOpacity={0.05} stroke={h.color} strokeOpacity={0.35} />
                {h.showName && (
                  <text x={sx} y={sy - sr - 4} textAnchor="middle" fill={h.color} fontSize={11}>
                    {name}
                  </text>
                )}
              </g>
            );
          })}
          {(scene.orbitRings ?? []).map((ring) => {
            const sx = ring.cx * cam.k + cam.x;
            const sy = ring.cy * cam.k + cam.y;
            const sr = ring.r * cam.k;
            return (
              <g key={ring.index} data-testid={`orbit-ring-${ring.index}`}>
                {sr > 1 && (
                  <circle cx={sx} cy={sy} r={sr} fill="none" stroke="currentColor" strokeOpacity={0.28} />
                )}
                <text x={sx} y={sy - sr - 6} textAnchor="middle" fill="currentColor" fontSize={11}>
                  {ring.label}
                </text>
              </g>
            );
          })}
          {segments.map((s, i) => {
            const x0 = s.x0 * cam.k + cam.x;
            const y0 = s.y0 * cam.k + cam.y;
            const x1 = s.x1 * cam.k + cam.x;
            const y1 = s.y1 * cam.k + cam.y;
            return (
              <g key={`${s.src}-${s.dst}-${s.type}-${i}`} data-testid="path-edge" data-edge-type={s.type}>
                <line x1={x0} y1={y0} x2={x1} y2={y1} stroke={s.color} strokeWidth={2.5} />
                <text x={(x0 + x1) / 2} y={(y0 + y1) / 2 - 6} textAnchor="middle" fill={s.color} fontSize={10}>
                  {s.type}
                </text>
              </g>
            );
          })}
        </svg>
      )}
      <div className="pointer-events-none absolute inset-0">
        {labels.map((id) => {
          const i = layout ? layout.ids.indexOf(id) : -1;
          if (!layout || i < 0) return null;
          const sx = layout.x[i] * cam.k + cam.x;
          const sy = layout.y[i] * cam.k + cam.y;
          return (
            <span
              key={id}
              data-testid={`label-${id}`}
              className="absolute whitespace-nowrap font-mono text-small text-muted"
              style={{ left: sx + 8, top: sy - 8 }}
            >
              {shortName(nameOf.get(id), id)}
            </span>
          );
        })}
        {tip && (
          <div
            data-testid="canvas-tip"
            className="absolute rounded border border-line-2 bg-surface px-1.5 py-0.5 font-mono text-small text-fg"
            style={{ left: tip.x, top: tip.y }}
          >
            {tip.text}
          </div>
        )}
      </div>

      {tracing && <PathBanner pending={!!pathState?.pending} slow={!!pathState?.slow} />}
      <div className="absolute left-3 top-3 z-10 flex flex-col items-start gap-1.5">
        <LayoutChips layout={layoutKind} onLayout={chooseLayout} returnTo={returnTo} />
        {scene && (
          <p data-testid="lod-caption" className="max-w-[440px] rounded bg-surface/80 px-2 py-1 text-[11.5px] text-muted">
            {scene.caption}
          </p>
        )}
        {truncated && (
          <p data-testid="truncated-caption" className="rounded bg-surface/80 px-2 py-1 text-[11.5px] text-fg">
            Showing {groupThousands(shown)} of {groupThousands(totalNodes)} nodes (truncated)
          </p>
        )}
        {area.slow && (
          <p className="rounded bg-surface/80 px-2 py-1 text-[11.5px] text-muted">Layout took over 1s.</p>
        )}
      </div>

    </div>
  );
}

/** Re-exported so a frame test can assert culling without mounting the view. */
export { worldInView };
