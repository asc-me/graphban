/**
 * Edge level of detail (PRD-52 D5).
 *
 * Below 2.6× the frame draws one bundle per area pair. At 2.6× or more it
 * draws individual edges, after the type filter and Isolate, capped at 6,000.
 * The rank is computed from that filtered set, not from the camera.
 */

import type { Bundle, Hull, RankedEdge } from "@/lib/graph/bigLayout";
import { bundleLabel } from "@/lib/graph/bigLayout";
import type { Cam } from "./camera";

export const LOD_ZOOM = 2.6;
export const EDGE_CAP = 6000;

export const EDGE_COLOR: Record<string, string> = {
  imports: "#7ca2ff",
  calls: "#c6f24e",
  owns: "#a78bfa",
  tested_by: "#5fd07a",
  references: "#e0b34a",
};

export type EdgeLod = "bundles" | "edges";

export function edgeLod(zoom: number): EdgeLod {
  return zoom >= LOD_ZOOM ? "edges" : "bundles";
}

export function groupThousands(n: number): string {
  const s = String(Math.trunc(Math.abs(n)));
  const body = s.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return n < 0 ? `-${body}` : body;
}

/** The sentence the cap shows. `shown` is what was drawn, `total` is in view before the cap. */
export function capCaption(shown: number, total: number): string {
  return `${groupThousands(shown)} of ${groupThousands(total)} edges in view: densest first. Zoom in, filter types, or Isolate.`;
}

export interface Quad {
  x0: number;
  y0: number;
  cx: number;
  cy: number;
  x1: number;
  y1: number;
}

/** Where a ray from the hull centre along `(ux, uy)` meets the hull. A Map tile uses its rect. */
function rimPoint(h: Hull, ux: number, uy: number, sign: number): { x: number; y: number } {
  if (h.rect && h.rect.w > 0 && h.rect.h > 0) {
    const hw = h.rect.w / 2;
    const hh = h.rect.h / 2;
    const tx = Math.abs(ux) < 1e-9 ? Infinity : hw / Math.abs(ux);
    const ty = Math.abs(uy) < 1e-9 ? Infinity : hh / Math.abs(uy);
    const t = Math.min(tx, ty);
    return { x: h.cx + ux * t * sign, y: h.cy + uy * t * sign };
  }
  return { x: h.cx + ux * h.r * sign, y: h.cy + uy * h.r * sign };
}

/**
 * Quadratic between hull edges. Bend 0.1, control point from centres and radii only.
 *
 * Map tiles are the exception: they are packed edge to edge, so rim to rim is a stub a few
 * pixels long between neighbours. Tiles connect centre to centre at the design's Map bend
 * (0.08), drawn over the tiles and under the nodes.
 */
export function quadraticBetween(a: Hull, b: Hull): Quad {
  const dx = b.cx - a.cx;
  const dy = b.cy - a.cy;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  if (a.rect && b.rect) {
    return {
      x0: a.cx,
      y0: a.cy,
      cx: (a.cx + b.cx) / 2 - uy * 0.08 * len,
      cy: (a.cy + b.cy) / 2 + ux * 0.08 * len,
      x1: b.cx,
      y1: b.cy,
    };
  }
  const p0 = rimPoint(a, ux, uy, 1);
  const p1 = rimPoint(b, ux, uy, -1);
  const x0 = p0.x;
  const y0 = p0.y;
  const x1 = p1.x;
  const y1 = p1.y;
  const mx = (x0 + x1) / 2;
  const my = (y0 + y1) / 2;
  return {
    x0,
    y0,
    cx: mx + -uy * 0.1 * len,
    cy: my + ux * 0.1 * len,
    x1,
    y1,
  };
}

export function quadPoint(q: Quad, t: number): { x: number; y: number } {
  const u = 1 - t;
  return {
    x: u * u * q.x0 + 2 * u * t * q.cx + t * t * q.x1,
    y: u * u * q.y0 + 2 * u * t * q.cy + t * t * q.y1,
  };
}

/** A hover within this many screen pixels of a bundle stroke counts. */
export const BUNDLE_HIT_PX = 10;

/**
 * The bundle under a world point, or null.
 *
 * Samples are spaced by the hit radius along the control polygon, which is at
 * least as long as the curve. A fixed 11-sample walk leaves gaps on a fitted
 * area pair (often longer than 200px), so the middle of the stroke never shows
 * the A→B / B→A tooltip.
 */
export function hitBundle(strokes: Stroke[], wx: number, wy: number, cam: Cam): Stroke | null {
  let best: { stroke: Stroke; d: number } | null = null;
  const k = Math.max(Math.abs(cam.k), 1e-6);
  for (const stroke of strokes) {
    if (stroke.kind !== "quad") continue;
    const span =
      (Math.hypot(stroke.cx - stroke.x0, stroke.cy - stroke.y0) +
        Math.hypot(stroke.x1 - stroke.cx, stroke.y1 - stroke.cy)) *
      k;
    const steps = Math.max(1, Math.ceil(span / BUNDLE_HIT_PX));
    for (let step = 0; step <= steps; step++) {
      const p = quadPoint(stroke, step / steps);
      const d = Math.hypot(p.x - wx, p.y - wy) * k;
      if (d <= BUNDLE_HIT_PX && (best === null || d < best.d)) best = { stroke, d };
    }
  }
  return best?.stroke ?? null;
}

export interface Stroke {
  kind: "quad" | "line";
  x0: number;
  y0: number;
  cx: number;
  cy: number;
  x1: number;
  y1: number;
  color: string;
  widthPx: number;
  /** Set when a split pair collapsed because both strokes were under 1.5px. */
  marker?: "double";
  label: string;
}

function strokeWidth(count: number): number {
  return Math.max(1, Math.log2(count + 1));
}

function shiftQuad(q: Quad, along: { x: number; y: number }, dist: number): Quad {
  return {
    x0: q.x0 + along.x * dist,
    y0: q.y0 + along.y * dist,
    cx: q.cx + along.x * dist,
    cy: q.cy + along.y * dist,
    x1: q.x1 + along.x * dist,
    y1: q.y1 + along.y * dist,
  };
}

function normalOf(a: Hull, b: Hull): { x: number; y: number } {
  const dx = b.cx - a.cx;
  const dy = b.cy - a.cy;
  const len = Math.hypot(dx, dy) || 1;
  return { x: -dy / len, y: dx / len };
}

/**
 * Bundles for the current type filter. Counts come from `edges` (already
 * type-filtered). Geometry comes only from the hulls.
 */
export function bundlesFromEdges(edges: RankedEdge[], hulls: Hull[]): Bundle[] {
  const hullById = new Map(hulls.map((h) => [h.id, h]));
  const rows = new Map<string, { a: string; b: string; ab: number; ba: number }>();
  for (const e of edges) {
    if (!e.srcArea || !e.dstArea || e.srcArea === e.dstArea) continue;
    const a = e.srcArea < e.dstArea ? e.srcArea : e.dstArea;
    const b = e.srcArea < e.dstArea ? e.dstArea : e.srcArea;
    const key = `${a}\0${b}`;
    let row = rows.get(key);
    if (!row) {
      row = { a, b, ab: 0, ba: 0 };
      rows.set(key, row);
    }
    if (e.srcArea === a) row.ab += 1;
    else row.ba += 1;
  }
  const out: Bundle[] = [];
  for (const row of [...rows.values()].sort((p, q) => (p.a < q.a ? -1 : p.a > q.a ? 1 : p.b < q.b ? -1 : p.b > q.b ? 1 : 0))) {
    const ha = hullById.get(row.a);
    const hb = hullById.get(row.b);
    if (!ha || !hb) continue;
    out.push({
      ...row,
      colorA: ha.color,
      colorB: hb.color,
      label: bundleLabel(ha.label, hb.label, row.ab, row.ba),
      hullA: ha,
      hullB: hb,
    });
  }
  return out;
}

/** One or two offset strokes for a bundle. 80% in one direction is a single stroke. */
export function bundleStrokes(bundle: Bundle, zoom: number): Stroke[] {
  const total = bundle.ab + bundle.ba;
  if (total <= 0) return [];
  const q = quadraticBetween(bundle.hullA, bundle.hullB);
  const dominant = Math.max(bundle.ab, bundle.ba) / total;
  const both = bundle.ab > 0 && bundle.ba > 0 && dominant < 0.8;
  const wa = strokeWidth(bundle.ab);
  const wb = strokeWidth(bundle.ba);
  if (both && wa < 1.5 && wb < 1.5) {
    const forward = bundle.ab >= bundle.ba;
    return [{
      kind: "quad",
      ...q,
      color: forward ? bundle.colorA : bundle.colorB,
      widthPx: Math.max(wa, wb),
      marker: "double",
      label: bundle.label,
    }];
  }
  if (both) {
    const n = normalOf(bundle.hullA, bundle.hullB);
    const offA = (wa / 2 + 1) / Math.max(zoom, 0.05);
    const offB = (wb / 2 + 1) / Math.max(zoom, 0.05);
    return [
      { kind: "quad", ...shiftQuad(q, n, offA), color: bundle.colorA, widthPx: wa, label: bundle.label },
      { kind: "quad", ...shiftQuad(q, n, -offB), color: bundle.colorB, widthPx: wb, label: bundle.label },
    ];
  }
  const forward = bundle.ab >= bundle.ba;
  return [{
    kind: "quad",
    ...q,
    color: forward ? bundle.colorA : bundle.colorB,
    widthPx: strokeWidth(total),
    label: bundle.label,
  }];
}

export interface RankResult {
  kept: RankedEdge[];
  totalInView: number;
  capped: boolean;
}

interface RankMemo {
  types: Set<string> | null;
  isolate: Set<string> | null;
  ranked: RankedEdge[];
}

/**
 * Degree and sort do not read the camera. A pan used to sort the whole pool
 * again on every frame (dense is ~27k edges). The memo is keyed by the pool
 * array and the filter sets the caller passed, so a new Set still recomputes.
 * The array is not copied: mutating it in place would serve a stale rank.
 */
const rankMemo = new WeakMap<RankedEdge[], RankMemo>();

/**
 * Type filter, then Isolate, then viewport, then the 6,000 cap.
 * Rank: max endpoint degree, then the smaller one, then canonical index.
 * The rank is computed from that filtered set, not from the camera.
 */
export function rankAndCap(
  edges: RankedEdge[],
  opts: {
    types: Set<string> | null;
    isolate: Set<string> | null;
    inView: (id: string) => boolean;
    cap: number;
  },
): RankResult {
  const memo = rankMemo.get(edges);
  let ranked: RankedEdge[];
  if (memo && memo.types === opts.types && memo.isolate === opts.isolate) {
    ranked = memo.ranked;
  } else {
    let list = edges;
    if (opts.types) {
      const types = opts.types;
      list = list.filter((e) => types.has(e.type));
    }
    if (opts.isolate) {
      const iso = opts.isolate;
      list = list.filter((e) => iso.has(e.src) && iso.has(e.dst));
    }
    const deg = new Map<string, number>();
    for (const e of list) {
      deg.set(e.src, (deg.get(e.src) ?? 0) + 1);
      deg.set(e.dst, (deg.get(e.dst) ?? 0) + 1);
    }
    ranked = list.slice().sort((a, b) => {
      const amax = Math.max(deg.get(a.src) ?? 0, deg.get(a.dst) ?? 0);
      const bmax = Math.max(deg.get(b.src) ?? 0, deg.get(b.dst) ?? 0);
      if (amax !== bmax) return bmax - amax;
      const amin = Math.min(deg.get(a.src) ?? 0, deg.get(a.dst) ?? 0);
      const bmin = Math.min(deg.get(b.src) ?? 0, deg.get(b.dst) ?? 0);
      if (amin !== bmin) return bmin - amin;
      return a.index - b.index;
    });
    rankMemo.set(edges, { types: opts.types, isolate: opts.isolate, ranked });
  }
  const kept: RankedEdge[] = [];
  let totalInView = 0;
  for (const e of ranked) {
    if (!opts.inView(e.src) && !opts.inView(e.dst)) continue;
    totalInView += 1;
    if (kept.length < opts.cap) kept.push(e);
  }
  return { kept, totalInView, capped: totalInView > opts.cap };
}
