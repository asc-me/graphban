/**
 * One frame of the canvas, built from a cached layout.
 *
 * This module does not import `clustersLayout`. A camera change must not
 * recompute a position. Callers pass the alpha buffer they already wrote.
 */

import type { ClusterLayout, OrbitRing, RankedEdge, TileRect } from "@/lib/graph/bigLayout";
import { ORBIT_BUNDLE_CAP, topBundles } from "@/lib/graph/bigLayout";
import type { Cam } from "./camera";
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
  /** Orbit ring labels. Empty for Clusters and Map. */
  orbitRings?: OrbitRing[];
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
  /** Map tile under the pointer or selected, when no node is focused. */
  tileId?: string | null;
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
      square: screenR < SQUARE_PX || layout.kinds[i] === "doc" || layout.kinds[i] === "config",
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
    const pos = indexOf;
    for (const e of ranked.kept) {
      const ia = pos.get(e.src);
      const ib = pos.get(e.dst);
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
        glow.push({
          kind: "line",
          x0: layout.x[ia],
          y0: layout.y[ia],
          cx: 0,
          cy: 0,
          x1: layout.x[ib],
          y1: layout.y[ib],
          color: EDGE_COLOR[e.type] ?? "#8b949e",
          widthPx: 2.5,
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
    orbitRings: layout.orbitRings ?? [],
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
