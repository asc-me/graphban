/**
 * Canvas mode for a code map above DETAIL_BUDGET (PRD-52 D1, D4, D5).
 *
 * Clusters, Map and Orbit are drawings. Matrix is the DOM grid (PRD-52 D4)
 * and replaces this surface when that chip is on. WebGL2 draws the bitmap,
 * Canvas 2D is the fallback, and neither context shows the reduced Matrix.
 * Labels, hull names and the tooltip are DOM.
 */

import * as React from "react";
import { createPortal } from "react-dom";

import { zoomAbout } from "@/lib/graph/useGraphViewport";
import { pathAlphaBuffer } from "@/lib/graph/pathHighlight";
import { applyPinOverrides, fitTargets, hitTest, type AreaLayoutKind, type ClusterLayout } from "@/lib/graph/bigLayout";
import { useAreaLayout } from "@/lib/graph/useAreaLayout";
import {
  DRAG_PIN_PX,
  pinsFor,
  useCanvasPins,
  type CanvasPinsApi,
} from "@/lib/graph/useGraphPins";
import { useCanvasListbox, type HolderReading } from "@/lib/graph/useGraphKeyboard";
import { nodesHeldBy } from "@/lib/graph/presence";
import type { FleetPresence } from "@/lib/types";
import { canvasClouds } from "./canvas/clouds";
import { useAlphaFade } from "./canvas/fade";
import { AreaMatrix, LayoutChips, type GraphLayoutId } from "./AreaMatrix";
import type { CodePath } from "@/lib/types";
import { createCanvas2D } from "./canvas/canvas2d";
import { ReducedView } from "./canvas/reduced";
import { createWebGL, swapToCanvas2D } from "./canvas/webgl";
import {
  CANVAS_MAX_K,
  CANVAS_MIN_K,
  FOCUS_ZOOM,
  fitCamera,
  fitScale,
  zoomFloor,
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
  /** What an agent said the node is. The hover card's second line, as Build.dc.html draws it. */
  summary?: string;
}

/** Build.dc.html `gTip`: the description, cut at 46 characters. */
function tipSub(summary: string | undefined): string | undefined {
  if (!summary) return undefined;
  return summary.length > 46 ? `${summary.slice(0, 45)}…` : summary;
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

/** Room left either side of a Map tile's name. */
const TILE_NAME_PAD = 10;
/** Average advance of the 11px overlay face; a name past this many per pixel is cut. */
const TILE_CHAR_PX = 6.4;

/**
 * A Map tile's name, fitted to its width. Packed tiles are narrow, and a name wider than
 * its tile ran into the next one. The count goes first, then the name is cut with an
 * ellipsis; a tile with room for fewer than four characters shows none.
 */
export function fitTileName(label: string, count: number | null, widthPx: number): string | null {
  const room = Math.floor(widthPx / TILE_CHAR_PX);
  const full = count === null ? label : `${label} · ${count}`;
  if (full.length <= room) return full;
  if (label.length <= room) return label;
  if (room < 4) return null;
  return `${label.slice(0, room - 1)}…`;
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
  undescribed = 0,
  frameId,
  controlRef,
  onZoom,
  onZoomFloor,
  holderOf,
  pathPending = false,
  pathArmed = false,
  onAbortPath,
  onDisarmPath,
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
  onTraceTo,
  chromeSlots,
  canvasPins,
  presence = null,
  soloUser = null,
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
  /** Referenced by an edge but never described. Read aloud, as the caption shows it. */
  undescribed?: number;
  /** An external request (find, hubs) to tween this node to 7×. */
  frameId: string | null;
  /** The view's GraphControls cluster drives zoom and fit through this. */
  controlRef?: { current: CanvasControlApi | null };
  /** Fired when the camera scale changes, so the shared readout can follow. */
  onZoom?: (k: number) => void;
  /** The lowest scale this layout allows, so the zoom-out control stops where the camera does. */
  onZoomFloor?: (k: number) => void;
  /**
   * Who holds this node, if anyone looked. Omit it and the live region says
   * the holder is unknown — an empty reading would claim the node is free.
   */
  holderOf?: (id: string) => HolderReading;
  /** A path request is in flight. Escape aborts it and leaves the mode armed. */
  pathPending?: boolean;
  /** Path mode is armed and waiting for a target. Escape disarms it. */
  pathArmed?: boolean;
  onAbortPath?: () => void;
  onDisarmPath?: () => void;
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
  /** Alt-click on another node traces a path from the selection (PRD-20 AC-19). */
  onTraceTo?: (id: string) => void;
  /**
   * Where the view wants this surface's chrome. With a `status` slot the view draws the
   * layout chips itself, above find, and the toasts and status lines go under find; the
   * zoom caption goes in `caption`, inside the legend box. Omitted, all of it stays here.
   */
  chromeSlots?: { status: HTMLElement | null; caption: HTMLElement | null };
  /** Live reservations. The fleet legend solos through `soloUser`. */
  presence?: FleetPresence | null;
  /** Set by the fleet legend. That user's clouds stay; the others fade. */
  soloUser?: string | null;
  /**
   * Session pins. The view holds them so Matrix, which unmounts this surface,
   * does not drop them. Omitted, the canvas keeps its own store.
   */
  canvasPins?: CanvasPinsApi;
}) {
  const hostRef = React.useRef<HTMLDivElement | null>(null);
  const listboxRef = React.useRef<HTMLDivElement | null>(null);
  const glRef = React.useRef<HTMLCanvasElement | null>(null);
  const d2Ref = React.useRef<HTMLCanvasElement | null>(null);
  const rendererRef = React.useRef<Renderer | null>(null);
  const sceneRef = React.useRef<DrawScene | null>(null);
  const [kind, setKind] = React.useState<"pending" | "webgl" | "canvas2d" | "none">("pending");
  const [size, setSize] = React.useState({ w: 900, h: 560 });
  const [cam, setCam] = React.useState<Cam>({ x: 0, y: 0, k: 1 });
  const [tween, setTween] = React.useState<Tween | null>(null);
  const [hover, setHover] = React.useState<string | null>(null);
  const [hoverTile, setHoverTile] = React.useState<string | null>(null);
  const [selectedTile, setSelectedTile] = React.useState<string | null>(null);
  const [tip, setTip] = React.useState<{ x: number; y: number; text: string; sub?: string } | null>(null);
  const [cameraOwner, setCameraOwner] = React.useState<"fit" | "pair" | "area" | "node">("fit");
  const camRef = React.useRef(cam);
  camRef.current = cam;
  const sizeRef = React.useRef(size);
  sizeRef.current = size;
  const fitted = React.useRef("");
  const [localLayout, setLocalLayout] = React.useState<GraphLayoutId>("clusters");
  const layoutMode: GraphLayoutId = onLayout ? (layoutProp ?? "clusters") : localLayout;
  const ownedPins = useCanvasPins();
  const pinsApi = canvasPins ?? ownedPins;
  const readPinsRef = React.useRef(pinsApi.read);
  readPinsRef.current = pinsApi.read;
  const layoutModeRef = React.useRef(layoutMode);
  layoutModeRef.current = layoutMode;
  const [drag, setDrag] = React.useState<{ id: string; x: number; y: number } | null>(null);
  const [toast, setToast] = React.useState<string | null>(null);
  const gesture = React.useRef<{
    nodeId: string | null;
    px: number;
    py: number;
    cx: number;
    cy: number;
    dragging: boolean;
  } | null>(null);
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

  // Below 0.7 only when the whole layout needs it (a tall Map treemap does).
  const minK = React.useMemo(
    () => (layout ? zoomFloor(fitScale(fitTargets(layout, {}), size.w, size.h)) : CANVAS_MIN_K),
    [layout, size.w, size.h],
  );
  const minKRef = React.useRef(minK);
  minKRef.current = minK;
  React.useEffect(() => {
    onZoomFloor?.(minK);
  }, [minK, onZoomFloor]);

  const frameToFit = React.useCallback(() => {
    const lay = layoutRef.current;
    const { w, h } = sizeRef.current;
    if (!lay) return;
    setTween(null);
    setCam(
      fitCamera(fitTargets(lay, pinsFor(readPinsRef.current(), layoutModeRef.current)), w, h, 48, minKRef.current),
    );
  }, []);

  React.useEffect(() => {
    onZoom?.(cam.k);
  }, [cam.k, onZoom]);

  React.useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 5000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  React.useEffect(() => {
    if (!controlRef) return;
    controlRef.current = {
      zoomBy(factor) {
        const { w, h } = sizeRef.current;
        setTween(null);
        setCam((c) =>
          zoomAbout(c, factor, w / 2, h / 2, { min: minKRef.current, max: CANVAS_MAX_K }),
        );
      },
      fit() {
        frameToFit();
      },
    };
    return () => {
      controlRef.current = null;
    };
  }, [controlRef, frameToFit]);

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

  // Before the fit and the node frame. A regroup drops pins into the ref
  // synchronously, so both read the positions that are still pinned. The
  // overlay from this render still has the pin, and neither effect runs
  // again when that state lands.
  React.useEffect(() => {
    if (!layout) return;
    const index = new Map(layout.ids.map((id, i) => [id, i]));
    const cleared = pinsApi.reconcile((id) => {
      const at = index.get(id);
      return at === undefined ? undefined : layout.areaOf[at];
    });
    if (cleared > 0) setToast(`${cleared} pins cleared: areas regrouped`);
  }, [layout, pinsApi.reconcile]);

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
    setCam(
      fitCamera(
        fitTargets(layout, pinsFor(readPinsRef.current(), layoutModeRef.current)),
        size.w,
        size.h,
        48,
        minKRef.current,
      ),
    );
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
    //
    // Read the ref reconcile just wrote, not the overlay from this render.
    // With Find still armed, framing that overlay tweens to the pin a regroup
    // has already dropped, while the node draws back in its packed cell.
    if (pairFocus || frameAreaId || !frameId || !layout) return;
    const lay = applyPinOverrides(
      layout,
      pinsFor(readPinsRef.current(), layoutModeRef.current),
    );
    const i = lay.ids.indexOf(frameId);
    if (i < 0) return;
    const to = framePoint(lay.x[i], lay.y[i], FOCUS_ZOOM, size.w, size.h);
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

  const followNode = React.useCallback((id: string) => {
    const host = hostRef.current;
    const packed = layoutRef.current;
    if (!host || !packed) return;
    // Arrow keys frame the pin. layoutRef is the packed cell the drag left;
    // the overlay is what is drawn and what a click already hit-tests.
    const layoutNow = applyPinOverrides(
      packed,
      pinsFor(readPinsRef.current(), layoutModeRef.current),
    );
    const i = layoutNow.ids.indexOf(id);
    if (i < 0) return;
    const { w, h } = sizeRef.current;
    const c = camRef.current;
    if (worldInView(layoutNow.x[i], layoutNow.y[i], layoutNow.r[i], c, w, h)) {
      host.dataset.follow = "in-view";
      // Already in frame. The previous key may still be flying toward the node
      // just left; leaving that tween running moves the camera after the listbox.
      setTween(null);
      return;
    }
    const to = framePoint(layoutNow.x[i], layoutNow.y[i], FOCUS_ZOOM, w, h);
    const reduced = prefersReducedMotion();
    const next = replaceTween(c, to, performance.now(), reduced);
    // Same-turn keys read this. The render below copies state back into the ref.
    camRef.current = next.cam;
    const n = Number(host.dataset.followCount ?? "0") + 1;
    host.dataset.follow = id;
    host.dataset.followCount = String(n);
    setCam(next.cam);
    setTween(next.tween);
  }, []);

  const listNodes = React.useMemo(() => {
    const d = new Map<string, number>();
    for (const n of nodes) d.set(n.id, 0);
    for (const e of edges) {
      if (!types.has(e.type)) continue;
      if (d.has(e.src)) d.set(e.src, (d.get(e.src) ?? 0) + 1);
      if (e.dst !== e.src && d.has(e.dst)) d.set(e.dst, (d.get(e.dst) ?? 0) + 1);
    }
    return nodes.map((n) => ({
      id: n.id,
      path: n.path || n.id,
      kind: n.kind,
      name: n.name,
      degree: d.get(n.id) ?? 0,
    }));
  }, [nodes, edges, types]);

  const listId = React.useId();
  const kb = useCanvasListbox({
    nodes: listNodes,
    selectedId,
    onSelect: (id) => onSelect(id),
    onClearSelection: () => onSelect(null),
    onFollow: followNode,
    holderOf,
    pathPending,
    pathArmed,
    onAbortPath,
    onDisarmPath,
    idPrefix: listId,
  });

  // A pair draws both areas, not one node. Otherwise the pointer wins, then
  // the keyboard cursor, then the selection.
  const focusId = pairFocus ? null : hover ?? kb.activeId ?? selectedId;
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
  const pinLayer = pinsFor(pinsApi.byLayout, layoutMode);
  const overrides = React.useMemo(() => {
    if (!drag) return pinLayer;
    return {
      ...pinLayer,
      [drag.id]: { x: drag.x, y: drag.y, areaKey: pinLayer[drag.id]?.areaKey ?? "" },
    };
  }, [pinLayer, drag]);
  const positioned = React.useMemo(
    () => (layout ? applyPinOverrides(layout, overrides) : null),
    [layout, overrides],
  );
  const drawn = React.useMemo(() => {
    if (!positioned) return null;
    if (!pairFocus) return positioned;
    const edges = positioned.edges.filter(
      (e) => types.has(e.type) && e.srcArea === pairFocus.src && e.dstArea === pairFocus.dst,
    );
    return { ...positioned, edges };
  }, [positioned, pairFocus, types]);
  const pinIds = React.useMemo(() => {
    const ids = new Set(Object.keys(pinLayer));
    if (drag) ids.add(drag.id);
    return ids;
  }, [pinLayer, drag]);
  // The path buffer replaces the reach buffer. It is keyed on the path, not the
  // camera, so a pan does not write it again.
  const pathAlpha = React.useMemo(
    () => (layout && pathState?.path ? pathAlphaBuffer(layout.ids, pathState.path) : null),
    [layout, pathState?.path],
  );
  const soloIds = React.useMemo(
    () => (soloUser ? nodesHeldBy(presence ?? undefined, soloUser) : null),
    [presence, soloUser],
  );
  const alpha = React.useMemo(() => {
    if (!layout) return null;
    if (pathAlpha) return pathAlpha;
    // Solo beats a selection, the same order the SVG view uses. Hover still
    // wins, so a soloed teammate does not stop you reading a neighbour.
    if (soloIds && !hover) {
      const out = new Uint8Array(layout.ids.length);
      const dim = Math.round(0.22 * 255);
      for (let i = 0; i < layout.ids.length; i++) out[i] = soloIds.has(layout.ids[i]) ? 255 : dim;
      return out;
    }
    return writeAlpha(layout.ids.length, reach, isolate && !!reach);
  }, [layout, pathAlpha, soloIds, hover, reach, isolate]);
  const segments = React.useMemo(
    () => (drawn && pathState?.path ? pathSegments(pathState.path, drawn.ids, drawn.x, drawn.y) : []),
    [drawn, pathState?.path],
  );
  const tracing = !!(pathState?.picking || pathState?.pending);
  const isolateSet = React.useMemo(() => {
    if (!isolate || !reach || !layout) return null;
    const set = new Set<string>();
    for (let i = 0; i < layout.ids.length; i++) if (reach[i]) set.add(layout.ids[i]);
    return set;
  }, [isolate, reach, layout]);

  const dpr = typeof window === "undefined" ? 1 : Math.min(2, window.devicePixelRatio || 1);
  const clouds = React.useMemo(
    () => (drawn ? canvasClouds(drawn, presence, cam.k, soloUser) : []),
    [drawn, presence, cam.k, soloUser],
  );
  // Eased toward `alpha`, so a hover or a selection fades in instead of snapping.
  const faded = useAlphaFade(alpha, pathAlpha ? null : (hover ?? selectedId));
  const scene = React.useMemo(() => {
    if (!drawn || !faded.alpha) return null;
    const built = buildScene(drawn, {
      camera: cam,
      width: size.w,
      height: size.h,
      dpr,
      alpha: faded.alpha,
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
      pinned: pinIds,
      presence,
      clouds,
      reducedMotion: prefersReducedMotion(),
    });
    return { ...built, focusMix: faded.mix };
  }, [drawn, faded, pathAlpha, cam, size.w, size.h, dpr, types, isolateSet, selectedId, hover, hoverTile, selectedTile, pinIds, presence, clouds]);

  sceneRef.current = scene;

  React.useEffect(() => {
    const glCanvas = glRef.current;
    const d2 = d2Ref.current;
    if (!glCanvas || !d2) return;
    let gone = false;
    const gl = createWebGL(glCanvas, {
      onLost() {
        const next = swapToCanvas2D(glCanvas, d2, sceneRef.current);
        rendererRef.current = next;
        if (!gone) setKind(next ? "canvas2d" : "none");
      },
    });
    if (gl) {
      rendererRef.current = gl;
      d2.hidden = true;
      glCanvas.hidden = false;
      setKind("webgl");
      return () => {
        gone = true;
        gl.destroy();
      };
    }
    glCanvas.hidden = true;
    const fb = createCanvas2D(d2);
    if (fb) {
      rendererRef.current = fb;
      d2.hidden = false;
      setKind("canvas2d");
      return () => {
        gone = true;
        fb.destroy();
      };
    }
    d2.hidden = true;
    setKind("none");
    return () => {
      gone = true;
    };
  }, []);

  React.useEffect(() => {
    if (!scene || kind === "pending" || kind === "none" || !rendererRef.current) return;
    const renderer = rendererRef.current;
    const pulse =
      !scene.staticPresence && scene.rings.some((ring) => ring.kind === "contention");
    if (!pulse) {
      renderer.draw(scene);
      return;
    }
    let raf = 0;
    let dead = false;
    const tick = (now: number) => {
      if (dead) return;
      // 2.4s, the same hold-pulse as the SVG ring: 0.85 at the peaks, 0.3 at the trough.
      const alpha = 0.575 + 0.275 * Math.cos((now / 2400) * Math.PI * 2);
      renderer.draw({
        ...scene,
        rings: scene.rings.map((ring) => (ring.kind === "contention" ? { ...ring, alpha } : ring)),
      });
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      dead = true;
      cancelAnimationFrame(raf);
    };
  }, [scene, kind]);

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
    setCam((c) => zoomAbout(c, Math.exp(-e.deltaY * 0.0015), px, py, { min: minKRef.current, max: CANVAS_MAX_K }));
  };

  const move = (clientX: number, clientY: number, sceneNow: DrawScene | null, layoutNow: ClusterLayout | null) => {
    const w = toWorld(clientX, clientY);
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
      setTip({ x: w.sx + 12, y: w.sy - 8, text: shortName(node, hit), sub: tipSub(node?.summary) });
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
    if (!drawn || !scene) return [];
    const focus = hover ?? selectedId;
    const fi = focus ? drawn.ids.indexOf(focus) : -1;
    const neighbors: string[] = [];
    if (focus) {
      for (const e of drawn.edges) {
        if (!types.has(e.type)) continue;
        if (e.src === focus) neighbors.push(e.dst);
        else if (e.dst === focus) neighbors.push(e.src);
      }
    }
    const labelNodes: LabelNode[] = drawn.ids.map((id, i) => {
      const text = shortName(nameOf.get(id), id);
      const sx = drawn.x[i] * cam.k + cam.x;
      const sy = drawn.y[i] * cam.k + cam.y;
      return {
        id,
        area: drawn.areaOf[i],
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
      focusKind: fi >= 0 ? drawn.kinds[fi] : "file",
      zoom: cam.k,
    });
  }, [drawn, scene, hover, selectedId, cam.k, cam.x, cam.y, degree, nameOf, types]);

  const shown = nodes.length;
  const drawnEdges = edges.reduce((n, e) => n + (types.has(e.type) ? 1 : 0), 0);
  const edgeClause =
    drawnEdges === edges.length ? `${edges.length} edges` : `${drawnEdges} of ${edges.length} edges`;
  const pairEdges = pairFocus && drawn ? drawn.edges.length : 0;

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

  const statusLines = (
    <>
      {pinsApi.total > 0 && (
        <button
          type="button"
          data-testid="unpin-all"
          onClick={pinsApi.clearAll}
          title="Release every pinned node"
          className="rounded-lg border border-control bg-surface-2 px-2 py-1 text-[11.5px] text-muted"
        >
          Unpin all
        </button>
      )}
      {toast && (
        <p
          data-testid="canvas-pin-toast"
          role="status"
          className="rounded bg-surface/80 px-2 py-1 text-[11.5px] text-fg"
        >
          {toast}
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
    </>
  );
  const caption = scene ? (
    <p
      data-testid="lod-caption"
      className={
        chromeSlots?.caption
          ? "max-w-[440px] font-mono text-[10px] text-[#5c656e]"
          : "max-w-[440px] rounded bg-surface/80 px-2 py-1 text-[11.5px] text-muted"
      }
    >
      {scene.caption}
    </p>
  ) : null;
  let chrome: React.ReactNode;
  if (!chromeSlots) {
    chrome = (
      <div className="absolute left-3 top-3 z-20 flex flex-col items-start gap-1.5">
        <LayoutChips layout={layoutKind} onLayout={chooseLayout} returnTo={returnTo} />
        {statusLines}
        {caption}
      </div>
    );
  } else {
    // The view's slots mount a frame after this does; until then the chrome waits.
    const captionAt = chromeSlots.caption ?? chromeSlots.status;
    chrome = (
      <>
        {chromeSlots.status && createPortal(statusLines, chromeSlots.status)}
        {captionAt && createPortal(caption, captionAt)}
      </>
    );
  }

  return (
    <div
      ref={hostRef}
      data-testid="code-graph-canvas"
      data-renderer={kind}
      data-camera-k={cam.k.toFixed(3)}
      data-pair={pairFocus ? `${pairFocus.src}→${pairFocus.dst}` : undefined}
      data-pair-edges={pairFocus ? String(pairEdges) : undefined}
      data-frame-area={frameAreaId ?? undefined}
      data-frame={cameraOwner}
      data-depth={layout?.depth ?? ""}
      data-camera-x={cam.x}
      data-layout={layoutMode}
      data-tween={tween ? "1" : "0"}
      role="application"
      aria-label={
        `Code graph canvas: ${shown} nodes, ${edgeClause}` +
        (undescribed ? `, ${undescribed} referenced but not yet described. ` : ". ") +
        `${layoutKind[0].toUpperCase()}${layoutKind.slice(1)} layout. ` +
        "Arrow keys, Home, End and type-ahead move between nodes. Enter selects. Escape clears."
      }
      className={`absolute inset-0 touch-none${tracing ? " cursor-crosshair" : ""}`}
      onWheel={onWheel}
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        const target = e.target as HTMLElement;
        if (target.closest("button")) return;
        listboxRef.current?.focus({ preventScroll: true });
        const w = toWorld(e.clientX, e.clientY);
        // Tracing returns before a pin. A drag while armed or pending pans,
        // even when it started on a node, and the release picks the target.
        const hit = tracing ? null : nodeAtPointer(scene, drawn, w.x, w.y);
        gesture.current = {
          nodeId: hit,
          px: w.sx,
          py: w.sy,
          cx: cam.x,
          cy: cam.y,
          dragging: false,
        };
        setTween(null);
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        const g = gesture.current;
        const w = toWorld(e.clientX, e.clientY);
        if (g) {
          const dist = Math.hypot(w.sx - g.px, w.sy - g.py);
          if (g.nodeId && dist > DRAG_PIN_PX) {
            g.dragging = true;
            setDrag({ id: g.nodeId, x: w.x, y: w.y });
            setHover(g.nodeId);
            setTip(null);
            return;
          }
          // Empty canvas, an undrawn file, or a trace: pan. A press that
          // landed on a node and has not crossed the threshold stays put.
          if (!g.nodeId) {
            setCam((c) => ({ ...c, x: g.cx + (w.sx - g.px), y: g.cy + (w.sy - g.py) }));
          }
          return;
        }
        move(e.clientX, e.clientY, scene, drawn);
      }}
      onPointerUp={(e) => {
        const g = gesture.current;
        gesture.current = null;
        if (!g) return;
        const w = toWorld(e.clientX, e.clientY);
        if (g.dragging && g.nodeId) {
          const lay = layoutRef.current;
          if (lay) {
            const i = lay.ids.indexOf(g.nodeId);
            if (i >= 0) pinsApi.place(layoutModeRef.current, g.nodeId, w.x, w.y, lay.areaOf[i]);
          }
          setDrag(null);
          return;
        }
        setDrag(null);
        if (Math.hypot(w.sx - g.px, w.sy - g.py) >= DRAG_PIN_PX) return;
        // Hit-test the overlay. The cached layout still has the node in its
        // packed cell, so a click at the drop would miss it.
        const hit = nodeAtPointer(scene, drawn, w.x, w.y);
        if (tracing) {
          if (hit) onPathTarget?.(hit);
          else onPathEmpty?.();
          return;
        }
        if (hit && e.altKey && selectedId && hit !== selectedId && onTraceTo) {
          onTraceTo(hit);
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
      }}
      onDoubleClick={(e) => {
        if (tracing) return;
        if (!drawn) return;
        const w = toWorld(e.clientX, e.clientY);
        const hit = nodeAtPointer(scene, drawn, w.x, w.y);
        if (!hit) {
          frameToFit();
          return;
        }
        const i = drawn.ids.indexOf(hit);
        const to = framePoint(drawn.x[i], drawn.y[i], FOCUS_ZOOM, size.w, size.h);
        const next = replaceTween(camRef.current, to, performance.now(), prefersReducedMotion());
        setCam(next.cam);
        setTween(next.tween);
        onSelect(hit);
      }}
    >
      <div
        ref={listboxRef}
        role="listbox"
        tabIndex={0}
        aria-label="Graph nodes"
        aria-activedescendant={kb.activeOptionId ?? undefined}
        data-testid="graph-node-listbox"
        className="sr-only"
        onKeyDown={kb.onKeyDown}
      >
        {kb.rows.map((row) => (
          <div
            key={row.id}
            id={row.optionId}
            role="option"
            aria-selected={row.id === selectedId}
            aria-setsize={kb.setSize}
            aria-posinset={row.index + 1}
          >
            {row.label}
          </div>
        ))}
      </div>
      <div aria-live="polite" aria-atomic="true" data-testid="graph-live" className="sr-only">
        {kb.live}
      </div>
      {/* Hull and tile SHAPES sit under the canvas, which is transparent, so area
          bundles and nodes draw over them. Above it they covered the Map's links. The
          names stay in the overlay. */}
      {kind !== "none" && scene && (
        <svg className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden data-testid="hull-shapes">
          {scene.hulls.map((h) => {
            if (h.rect) {
              return (
                <rect
                  key={h.id}
                  data-testid={`hull-shape-${h.id}`}
                  x={h.rect.x * cam.k + cam.x}
                  y={h.rect.y * cam.k + cam.y}
                  width={h.rect.w * cam.k}
                  height={h.rect.h * cam.k}
                  rx={6}
                  fill={h.color}
                  fillOpacity={0.05}
                  stroke={h.color}
                  strokeOpacity={0.35}
                />
              );
            }
            return (
              <circle
                key={h.id}
                data-testid={`hull-shape-${h.id}`}
                cx={h.cx * cam.k + cam.x}
                cy={h.cy * cam.k + cam.y}
                r={h.r * cam.k}
                fill={h.color}
                fillOpacity={0.05}
                stroke={h.color}
                strokeOpacity={0.35}
              />
            );
          })}
        </svg>
      )}
      <canvas
        ref={glRef}
        data-testid="code-graph-webgl"
        hidden={kind !== "webgl" && kind !== "pending"}
        className="absolute inset-0 h-full w-full"
      />
      <canvas
        ref={d2Ref}
        data-testid="code-graph-2d"
        hidden={kind !== "canvas2d"}
        className="absolute inset-0 h-full w-full"
      />
      {kind === "none" && <ReducedView layout={layout} />}
      {kind !== "none" && scene && (
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
              const fitted = h.showName
                ? fitTileName(h.label, h.showCount ? h.count : null, rw - TILE_NAME_PAD)
                : null;
              return (
                <g key={h.id} data-testid={`hull-${h.id}`}>
                  {fitted && (
                    <text x={rx + rw / 2} y={ry + 14} textAnchor="middle" fill={h.color} fontSize={11}>
                      <title>{h.label}</title>
                      {fitted}
                    </text>
                  )}
                </g>
              );
            }
            return (
              <g key={h.id} data-testid={`hull-${h.id}`}>
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
      {kind !== "none" && (
      <div className="pointer-events-none absolute inset-0">
        {labels.map((id) => {
          const i = drawn ? drawn.ids.indexOf(id) : -1;
          if (!drawn || i < 0) return null;
          const sx = drawn.x[i] * cam.k + cam.x;
          const sy = drawn.y[i] * cam.k + cam.y;
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
            {tip.sub && <div className="font-sans text-muted">{tip.sub}</div>}
          </div>
        )}
      </div>
      )}

      {kind !== "none" && tracing && <PathBanner pending={!!pathState?.pending} slow={!!pathState?.slow} />}
      {chrome}

    </div>
  );
}

/** Re-exported so a frame test can assert culling without mounting the view. */
export { worldInView };
