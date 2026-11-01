/**
 * One frame of the canvas, built from a cached layout.
 *
 * This module does not import `clustersLayout`. A camera change must not
 * recompute a position. Callers pass the alpha buffer they already wrote.
 */

import type { ClusterLayout, OrbitRing, RankedEdge, TileRect } from "@/lib/graph/bigLayout";
import { ORBIT_BUNDLE_CAP, topBundles } from "@/lib/graph/bigLayout";
import type { FleetPresence } from "@/lib/types";
import { contentionOf, indexByNode } from "@/lib/graph/presence";
import type { Cam } from "./camera";
import { CONTENTION_COLOR, PULSE_MEAN, type Cloud } from "./clouds";
import {
  EDGE_CAP,
  EDGE_COLOR,
  bundleStrokes,
  bundlesFromEdges,
  capCaption,
  edgeLod,
  rankAndCap,
  type Stroke,
} from "./edges";

export const AREAS_ONLY = 10_000;
export const VIEW_MARGIN = 30;
export const SQUARE_PX = 2.2;

interface StrokeMemo {
  key: number;
  pos: number;
  strokes: Stroke[];
}

const strokeMemo = new WeakMap<RankedEdge[], StrokeMemo>();

function posStamp(layout: ClusterLayout): number {
  let h = layout.x.length | 0;
  for (let i = 0; i < layout.x.length; i++) {
    h = (Math.imul(h, 31) ^ (layout.x[i] * 10)) | 0;
    h = (Math.imul(h, 31) ^ (layout.y[i] * 10)) | 0;
  }
  return h;
}

function keptKey(kept: RankedEdge[]): number {
  let h = kept.length | 0;
  for (let i = 0; i < kept.length; i++) h = Math.imul(h, 31) ^ kept[i].index;
  return h;
}

/** Build.dc.html glow: a 1.3px core at 0.95 over a 5px wash at 0.18, both in the edge colour. */
export const GLOW_CORE_PX = 1.3;
export const GLOW_WASH_PX = 5;
export const GLOW_CORE_ALPHA = 0.95;
export const GLOW_WASH_ALPHA = 0.18;

export interface DrawNode {
  id: string;
  index: number;
  x: number;
  y: number;
  r: number;
  color: string;
  kind: string;
  alpha: number;
  square: boolean;
}

export interface DrawHull {
  id: string;
  cx: number;
  cy: number;
  r: number;
  color: string;
  label: string;
  count: number;
  showName: boolean;
  showCount: boolean;
  /** Map tile, in world space. Circle hulls leave it unset. */
  rect?: TileRect;
}

export interface DrawRing {
  x: number;
  y: number;
  r: number;
  color: string;
  kind: "contention" | "pin" | "selection";
  /** Omitted rings draw at 0.9. Contention uses the pulse, or the static mean. */
  alpha?: number;
}

export interface DrawScene {
  width: number;
  height: number;
  dpr: number;
  camera: Cam;
  nodes: DrawNode[];
  strokes: Stroke[];
  hulls: DrawHull[];
  rings: DrawRing[];
  /** "bundles" or "edges" — what the strokes are. */
  lod: "bundles" | "edges" | "areas";
  caption: string;
  areasOnly: boolean;
  /** Area whose members are drawn on a map above 10k nodes, or null. */
  revealed: string | null;
  /** Focused node's incident edges, capped at 320. Drawn above the rings. */
  glow: Stroke[];
  /** 0→1 while a highlight eases in. The glow is drawn at this share of its alpha. */
  focusMix?: number;
  /**
   * One alpha for the whole edge batch. A path sets 0.05 so the batch does not
   * decide, per edge, whether that edge is on the route.
   */
  edgesAlpha: number;
  /** Orbit ring labels. Empty for Clusters and Map. */
  orbitRings?: OrbitRing[];
  /** Presence blobs, already resolved for this zoom and solo. Empty is "nobody held". */
  clouds?: readonly Cloud[];
  /**
   * Contention rings stay at {@link PULSE_MEAN} instead of pulsing. Set under
   * prefers-reduced-motion. Absent means the canvas may animate them.
   */
  staticPresence?: boolean;
}

export interface SceneOpts {
  camera: Cam;
  width: number;
  height: number;
  dpr: number;
  /** 0–255 per layout index. Written by the caller when focus changes, not per frame. */
  alpha: Uint8Array;
  types: Set<string> | null;
  isolate: Set<string> | null;
  selectedId: string | null;
  hoverId: string | null;
  /** Overrides the batch alpha. Absent keeps the ordinary 0.85. */
  edgesAlpha?: number;
  /** Map tile under the pointer or selected, when no node is focused. */
  tileId?: string | null;
  /** Node ids that wear a pin ring at r+5. Positions come from the layout. */
  pinned?: ReadonlySet<string> | null;
  /** Live reservations. Contention rings come from this; clouds are passed in. */
  presence?: FleetPresence | null;
  /** Clouds for this frame. The canvas computes them so both renderers share one list. */
  clouds?: readonly Cloud[];
  /** Hold the contention ring at the pulse mean. The canvas sets this from matchMedia. */
  reducedMotion?: boolean;
}

export function worldInView(x: number, y: number, r: number, cam: Cam, w: number, h: number): boolean {
  const sx = x * cam.k + cam.x;
  const sy = y * cam.k + cam.y;
  const sr = r * cam.k;
  return sx + sr >= -VIEW_MARGIN && sx - sr <= w + VIEW_MARGIN && sy + sr >= -VIEW_MARGIN && sy - sr <= h + VIEW_MARGIN;
}

/**
 * The area a large map is zoomed into, or null when members stay hidden.
 * Above 10k nodes the default frame is areas and bundles only.
 */
export function revealedArea(layout: ClusterLayout, cam: Cam, width: number, height: number): string | null {
  if (layout.nodeCount <= AREAS_ONLY) return null;
  const cx = (width / 2 - cam.x) / cam.k;
  const cy = (height / 2 - cam.y) / cam.k;
  const hits = layout.areas.filter((a) => Math.hypot(cx - a.cx, cy - a.cy) <= a.r);
  if (hits.length !== 1) return null;
  const area = hits[0];
  const screenR = area.r * cam.k;
  if (screenR < Math.min(width, height) * 0.35) return null;
  return area.id;
}

export function buildScene(layout: ClusterLayout, opts: SceneOpts): DrawScene {
  const { camera, width, height } = opts;
  const large = layout.nodeCount > AREAS_ONLY;
  const revealed = large ? revealedArea(layout, camera, width, height) : null;
  const indexOf = new Map(layout.ids.map((id, i) => [id, i]));

  const nodes: DrawNode[] = [];
  const allow = (i: number) => {
    if (!large) return true;
    return revealed !== null && layout.areaOf[i] === revealed;
  };
  for (let i = 0; i < layout.ids.length; i++) {
    if (!allow(i)) continue;
    const x = layout.x[i];
    const y = layout.y[i];
    const r = layout.r[i];
    if (!worldInView(x, y, r, camera, width, height)) continue;
    const screenR = r * camera.k;
    nodes.push({
      id: layout.ids[i],
      index: i,
      x,
      y,
      r,
      color: layout.colors[i],
      kind: layout.kinds[i],
      alpha: (opts.alpha[i] ?? 255) / 255,
      // LOD only. Doc's square and config's hexagon come from kindShape; forcing
      // both kinds square here made that pair colour-only at every zoom (GRPH-1027).
      square: screenR < SQUARE_PX,
    });
  }

  const inViewId = (id: string) => {
    const i = indexOf.get(id);
    if (i === undefined) return false;
    return worldInView(layout.x[i], layout.y[i], layout.r[i], camera, width, height);
  };

  let strokes: Stroke[] = [];
  let lod: DrawScene["lod"] = "bundles";
  let caption = "Area bundles — zoom to 2.6× for individual edges";

  let pool = opts.types ? layout.edges.filter((e) => opts.types!.has(e.type)) : layout.edges;
  if (opts.isolate) {
    const iso = opts.isolate;
    pool = pool.filter((e) => iso.has(e.src) && iso.has(e.dst));
  }

  const kind = layout.layout ?? "clusters";
  const nodeFocus = opts.hoverId ?? opts.selectedId;
  const focusedNode = nodeFocus && indexOf.has(nodeFocus) ? nodeFocus : null;
  const bundlePool = () =>
    opts.types || opts.isolate ? bundlesFromEdges(pool, layout.areas) : layout.bundles;

  const pushLines = (id: string) => {
    lod = "edges";
    caption = "Individual edges";
    for (const e of pool) {
      if (e.src !== id && e.dst !== id) continue;
      const ia = indexOf.get(e.src);
      const ib = indexOf.get(e.dst);
      if (ia === undefined || ib === undefined) continue;
      strokes.push({
        kind: "line",
        x0: layout.x[ia],
        y0: layout.y[ia],
        cx: 0,
        cy: 0,
        x1: layout.x[ib],
        y1: layout.y[ib],
        color: EDGE_COLOR[e.type] ?? "#8b949e",
        widthPx: 1.25,
        label: `${e.src}→${e.dst}`,
      });
    }
  };

  if (kind === "map") {
    // Map never follows the zoom LOD. Nothing focused draws nothing.
    if (focusedNode) {
      pushLines(focusedNode);
    } else if (opts.tileId && layout.areas.some((a) => a.id === opts.tileId)) {
      const tile = opts.tileId;
      caption = "Area bundles";
      for (const bundle of bundlePool().filter((b) => b.a === tile || b.b === tile)) {
        strokes.push(...bundleStrokes(bundle, camera.k));
      }
    } else {
      caption = "Hover an area to see its dependencies";
    }
  } else if (large && !revealed) {
    lod = "areas";
    caption = "Large map: zoom into an area to see its files";
    const bundles = bundlePool();
    for (const bundle of bundles) strokes.push(...bundleStrokes(bundle, camera.k));
  } else if (kind === "orbit" && !focusedNode && edgeLod(camera.k) === "bundles") {
    const all = bundlePool();
    const top = topBundles(all, ORBIT_BUNDLE_CAP);
    for (const bundle of top) strokes.push(...bundleStrokes(bundle, camera.k));
    if (all.length > ORBIT_BUNDLE_CAP) caption = "Top 30 area bundles";
  } else if (edgeLod(camera.k) === "bundles") {
    const bundles = bundlePool();
    for (const bundle of bundles) strokes.push(...bundleStrokes(bundle, camera.k));
  } else {
    lod = "edges";
    const ranked = rankAndCap(pool, {
      types: null,
      isolate: null,
      inView: inViewId,
      cap: EDGE_CAP,
    });
    caption = ranked.capped
      ? capCaption(ranked.kept.length, ranked.totalInView)
      : "Individual edges";
    const key = keptKey(ranked.kept);
    const pos = posStamp(layout);
    const memo = strokeMemo.get(pool);
    // Same kept edges and the same packed positions: a pan must not allocate
    // 6,000 stroke objects. The pool array is the key, so a freshly filtered
    // pool (type filter, Isolate) misses and rebuilds.
    if (memo && memo.key === key && memo.pos === pos) {
      strokes = memo.strokes;
    } else {
      const built: Stroke[] = [];
      for (const e of ranked.kept) {
        const ia = indexOf.get(e.src);
        const ib = indexOf.get(e.dst);
        if (ia === undefined || ib === undefined) continue;
        built.push({
          kind: "line",
          x0: layout.x[ia],
          y0: layout.y[ia],
          cx: 0,
          cy: 0,
          x1: layout.x[ib],
          y1: layout.y[ib],
          color: EDGE_COLOR[e.type] ?? "#8b949e",
          widthPx: 1.25,
          label: `${e.src}→${e.dst}`,
        });
      }
      strokes = built;
      strokeMemo.set(pool, { key, pos, strokes });
    }
  }

  const hulls: DrawHull[] = layout.areas.map((a) => {
    const screen = a.rect ? Math.min(a.rect.w, a.rect.h) * camera.k : a.r * camera.k;
    return {
      id: a.id,
      cx: a.cx,
      cy: a.cy,
      r: a.r,
      color: a.color,
      label: a.label,
      count: a.count,
      showName: screen >= 36,
      showCount: screen >= 90,
      rect: a.rect,
    };
  });

  const rings: DrawRing[] = [];
  // Contention sits inside the pin (r+5) and the selection (r+7). Binary:
  // two agents of one user are not an alarm. Reduced motion is a static ring
  // at the pulse's mean opacity; otherwise the canvas pulses this alpha.
  if (opts.presence) {
    const held = indexByNode(opts.presence);
    for (const [id, holders] of held) {
      if (!contentionOf(holders).contended) continue;
      const i = indexOf.get(id);
      if (i === undefined || !allow(i)) continue;
      const x = layout.x[i];
      const y = layout.y[i];
      const r = layout.r[i];
      if (!worldInView(x, y, r + 3, camera, width, height)) continue;
      rings.push({
        x,
        y,
        r: r + 3,
        color: CONTENTION_COLOR,
        kind: "contention",
        alpha: opts.reducedMotion ? PULSE_MEAN : 0.9,
      });
    }
  }
  if (opts.pinned && opts.pinned.size > 0) {
    for (const id of opts.pinned) {
      const i = indexOf.get(id);
      if (i === undefined || !allow(i)) continue;
      const x = layout.x[i];
      const y = layout.y[i];
      const r = layout.r[i];
      if (!worldInView(x, y, r + 5, camera, width, height)) continue;
      rings.push({ x, y, r: r + 5, color: "#8b949e", kind: "pin" });
    }
  }
  const focus = opts.hoverId ?? opts.selectedId;
  const glow: Stroke[] = [];
  if (focus) {
    const i = indexOf.get(focus);
    if (i !== undefined && (!large || layout.areaOf[i] === revealed)) {
      rings.push({
        x: layout.x[i],
        y: layout.y[i],
        r: layout.r[i] + 7,
        color: layout.colors[i],
        kind: "selection",
      });
      for (const e of pool) {
        if (glow.length >= 320) break;
        if (e.src !== focus && e.dst !== focus) continue;
        const ia = indexOf.get(e.src);
        const ib = indexOf.get(e.dst);
        if (ia === undefined || ib === undefined) continue;
        // Build.dc.html `gGlow`: a quadratic (bend 0.14, 0.08 on Map) in the edge
        // type's colour. The core is 1.3px; the renderers lay a 5px wash under it.
        const x0 = layout.x[ia];
        const y0 = layout.y[ia];
        const x1 = layout.x[ib];
        const y1 = layout.y[ib];
        const bend = layout.layout === "map" ? 0.08 : 0.14;
        glow.push({
          kind: "quad",
          x0,
          y0,
          cx: (x0 + x1) / 2 - (y1 - y0) * bend,
          cy: (y0 + y1) / 2 + (x1 - x0) * bend,
          x1,
          y1,
          color: EDGE_COLOR[e.type] ?? "#8b949e",
          widthPx: GLOW_CORE_PX,
          label: "",
        });
      }
    }
  }

  return {
    width,
    height,
    dpr: Math.min(2, Math.max(1, opts.dpr)),
    camera,
    nodes,
    strokes,
    hulls,
    rings,
    lod,
    caption,
    areasOnly: large && !revealed,
    revealed,
    glow,
    edgesAlpha: opts.edgesAlpha ?? 0.85,
    orbitRings: layout.orbitRings ?? [],
    clouds: opts.clouds ?? [],
    staticPresence: !!opts.reducedMotion,
  };
}

/** 2-hop reach, capped at 800 nodes. The alpha buffer is filled from this, once per focus change. */
export function reachOf(layout: ClusterLayout, focusId: string | null, edges: RankedEdge[]): Uint8Array | null {
  if (!focusId) return null;
  const index = new Map(layout.ids.map((id, i) => [id, i]));
  const start = index.get(focusId);
  if (start === undefined) return null;
  const adj = new Map<string, string[]>();
  for (const e of edges) {
    const a = adj.get(e.src);
    if (a) a.push(e.dst);
    else adj.set(e.src, [e.dst]);
    const b = adj.get(e.dst);
    if (b) b.push(e.src);
    else adj.set(e.dst, [e.src]);
  }
  const seen = new Set<string>([focusId]);
  let frontier = [focusId];
  for (let hop = 0; hop < 2 && seen.size < 800; hop++) {
    const next: string[] = [];
    for (const id of frontier) {
      for (const nb of adj.get(id) ?? []) {
        if (seen.has(nb)) continue;
        seen.add(nb);
        next.push(nb);
        if (seen.size >= 800) break;
      }
      if (seen.size >= 800) break;
    }
    frontier = next;
  }
  const out = new Uint8Array(layout.ids.length);
  for (const id of seen) {
    const i = index.get(id);
    if (i !== undefined) out[i] = 1;
  }
  return out;
}

let alphaWrites = 0;

export function alphaWriteCount(): number {
  return alphaWrites;
}

export function resetAlphaWrites(): void {
  alphaWrites = 0;
}

/** Out of reach dims to 0.22, or 0.06 under Isolate. Full opacity when nothing is focused. */
export function writeAlpha(n: number, reach: Uint8Array | null, isolate: boolean): Uint8Array {
  alphaWrites += 1;
  const out = new Uint8Array(n);
  if (!reach) {
    out.fill(255);
    return out;
  }
  const dim = isolate ? Math.round(0.06 * 255) : Math.round(0.22 * 255);
  for (let i = 0; i < n; i++) out[i] = reach[i] ? 255 : dim;
  return out;
}
