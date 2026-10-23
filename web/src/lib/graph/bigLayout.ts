/**
 * Clusters layout for canvas mode (PRD-52 D4).
 *
 * Computed once per map revision, off the main thread when a worker exists.
 * Pan and zoom only transform the arrays this returns — they must not call
 * `clustersLayout` again. Positions are deterministic: canonical node and edge
 * order, a seeded circle, a fixed relaxation. No `Math.random`.
 */

import {
  areaColor,
  areasAtScale,
  canonicalEdges,
  canonicalNodes,
  type ScaleEdge,
  type ScaleNode,
} from "./areas";

export const GRID_CELL = 10;
/** Hit-testing searches at most this many cells on a side. */
export const HIT_WINDOW = 4;
export const WORLD_W = 1600;
export const WORLD_H = 1000;

const KIND_RANK: Record<string, number> = {
  module: 0,
  file: 1,
  symbol: 2,
  doc: 3,
  config: 4,
};

export interface Hull {
  id: string;
  cx: number;
  cy: number;
  r: number;
  color: string;
  count: number;
  label: string;
  flat?: boolean;
}

export interface RankedEdge {
  src: string;
  dst: string;
  type: string;
  /** Index in the canonical non-owns edge list. Stable under input shuffle. */
  index: number;
  srcArea: string;
  dstArea: string;
}

export interface Bundle {
  a: string;
  b: string;
  ab: number;
  ba: number;
  colorA: string;
  colorB: string;
  label: string;
  hullA: Hull;
  hullB: Hull;
}

export interface ClusterInput {
  nodes: { id: string; path: string; kind: string }[];
  edges: { src: string; dst: string; type: string }[];
  prevDepth?: number;
}

export interface ClusterLayout {
  ids: string[];
  x: Float32Array;
  y: Float32Array;
  r: Float32Array;
  kinds: string[];
  colors: string[];
  areaOf: string[];
  areas: Hull[];
  bundles: Bundle[];
  edges: RankedEdge[];
  grid: Record<string, number[]>;
  depth: number | null;
  /** How many nodes the layout placed. Above 10_000 the view draws areas only. */
  nodeCount: number;
}

let layoutRuns = 0;

export function layoutRunCount(): number {
  return layoutRuns;
}

export function resetLayoutRuns(): void {
  layoutRuns = 0;
}

function hashText(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (Math.imul(h, 31) + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

function byText(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

export function nodeRadius(kind: string, degree: number): number {
  if (kind === "module") return 11 + Math.min(degree, 8) * 0.2;
  if (kind === "file") return 5.5 + Math.min(degree, 6) * 0.45;
  if (kind === "symbol") return 5 + Math.min(degree, 8) * 0.15;
  if (kind === "doc" || kind === "config") return 5.5;
  return 6 + Math.min(degree, 6) * 0.2;
}

function pairKey(a: string, b: string): string {
  return a < b ? `${a}\0${b}` : `${b}\0${a}`;
}

interface LocalPack {
  id: string;
  kind: string;
  color: string;
  members: { id: string; kind: string; lx: number; ly: number; r: number }[];
  r: number;
}

function packArea(
  id: string,
  memberIds: string[],
  kindOf: Map<string, string>,
  degree: Map<string, number>,
): LocalPack {
  const color = areaColor(id);
  const members = memberIds.map((mid) => ({
    id: mid,
    kind: kindOf.get(mid) ?? "file",
    degree: degree.get(mid) ?? 0,
  }));
  members.sort((a, b) => {
    const ka = KIND_RANK[a.kind] ?? 9;
    const kb = KIND_RANK[b.kind] ?? 9;
    if (ka !== kb) return ka - kb;
    if (a.degree !== b.degree) return b.degree - a.degree;
    return byText(a.id, b.id);
  });
  const placed: LocalPack["members"] = [];
  const center = members[0];
  if (center) {
    placed.push({
      id: center.id,
      kind: center.kind,
      lx: 0,
      ly: 0,
      r: nodeRadius(center.kind, center.degree),
    });
  }
  let ring = 1;
  let slot = 0;
  const slotsFor = (rn: number) => 8 + (rn - 1) * 4;
  let cap = slotsFor(1);
  for (let i = 1; i < members.length; i++) {
    const m = members[i];
    const ang = -Math.PI / 2 + (slot / cap) * Math.PI * 2;
    const rad = ring * 22;
    placed.push({
      id: m.id,
      kind: m.kind,
      lx: Math.cos(ang) * rad,
      ly: Math.sin(ang) * rad,
      r: nodeRadius(m.kind, m.degree),
    });
    slot += 1;
    if (slot >= cap) {
      ring += 1;
      slot = 0;
      cap = slotsFor(ring);
    }
  }
  let r = 28;
  for (const m of placed) r = Math.max(r, Math.hypot(m.lx, m.ly) + m.r + 16);
  return { id, kind: kindOf.get(id) ?? "", color, members: placed, r };
}

function relax(
  packs: LocalPack[],
  weights: Map<string, number>,
): { x: number; y: number }[] {
  const n = packs.length;
  const seed = hashText(packs.map((p) => p.id).join("|"));
  const rot = ((seed % 360) * Math.PI) / 180;
  const base = 48 + n * 36;
  const pos = packs.map((_, i) => {
    const ang = (i / Math.max(n, 1)) * Math.PI * 2 - Math.PI / 2 + rot;
    return { x: Math.cos(ang) * base, y: Math.sin(ang) * base };
  });
  const iterations = 48;
  for (let iter = 0; iter < iterations; iter++) {
    const fx = new Float64Array(n);
    const fy = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        let dx = pos[j].x - pos[i].x;
        let dy = pos[j].y - pos[i].y;
        let dist = Math.hypot(dx, dy);
        if (dist < 1e-3) {
          dx = 1;
          dy = 0;
          dist = 1;
        }
        const ux = dx / dist;
        const uy = dy / dist;
        const minDist = packs[i].r + packs[j].r + 28;
        if (dist < minDist) {
          const push = (minDist - dist) * 0.5;
          fx[i] -= ux * push;
          fy[i] -= uy * push;
          fx[j] += ux * push;
          fy[j] += uy * push;
        }
        const w = weights.get(pairKey(packs[i].id, packs[j].id)) ?? 0;
        if (w > 0) {
          const pull = Math.log(1 + w) * 0.35;
          fx[i] += ux * pull;
          fy[i] += uy * pull;
          fx[j] -= ux * pull;
          fy[j] -= uy * pull;
        }
      }
    }
    for (let i = 0; i < n; i++) {
      const mag = Math.hypot(fx[i], fy[i]);
      const scale = mag > 12 ? 12 / mag : 1;
      pos[i].x += fx[i] * scale;
      pos[i].y += fy[i] * scale;
    }
  }
  return pos;
}

function fit(
  pos: { x: number; y: number }[],
  radii: number[],
): { pos: { x: number; y: number }[]; scale: number } {
  if (pos.length === 0) return { pos, scale: 1 };
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (let i = 0; i < pos.length; i++) {
    const r = radii[i] ?? 0;
    minX = Math.min(minX, pos[i].x - r);
    maxX = Math.max(maxX, pos[i].x + r);
    minY = Math.min(minY, pos[i].y - r);
    maxY = Math.max(maxY, pos[i].y + r);
  }
  const bw = Math.max(1, maxX - minX);
  const bh = Math.max(1, maxY - minY);
  const pad = 48;
  const scale = Math.min((WORLD_W - pad * 2) / bw, (WORLD_H - pad * 2) / bh);
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  return {
    scale,
    pos: pos.map((p) => ({
      x: WORLD_W / 2 + (p.x - cx) * scale,
      y: WORLD_H / 2 + (p.y - cy) * scale,
    })),
  };
}

export function layoutHash(layout: ClusterLayout): string {
  let h = 2166136261;
  for (let i = 0; i < layout.ids.length; i++) {
    const qx = Math.round(layout.x[i] * 1000);
    const qy = Math.round(layout.y[i] * 1000);
    h = Math.imul(h ^ qx, 16777619);
    h = Math.imul(h ^ qy, 16777619);
    h = Math.imul(h ^ layout.ids[i].length, 16777619);
  }
  return (h >>> 0).toString(16);
}

export function clustersLayout(input: ClusterInput): ClusterLayout {
  layoutRuns += 1;
  const nodes = canonicalNodes(input.nodes as ScaleNode[]);
  const edges = canonicalEdges(input.edges as ScaleEdge[]);
  const owns = edges.filter((e) => e.type === "owns");
  const areas = areasAtScale(nodes, owns, input.prevDepth);
  const kindOf = new Map(nodes.map((n) => [n.id, n.kind]));
  const degree = new Map<string, number>();
  for (const n of nodes) degree.set(n.id, 0);
  for (const e of edges) {
    if (e.type === "owns") continue;
    degree.set(e.src, (degree.get(e.src) ?? 0) + 1);
    degree.set(e.dst, (degree.get(e.dst) ?? 0) + 1);
  }
  const areaOfId = new Map<string, string>();
  for (const area of areas.areas) {
    for (const id of area.members) areaOfId.set(id, area.id);
  }
  const packs = areas.areas.map((area) => packArea(area.id, area.members, kindOf, degree));
  const weights = new Map<string, number>();
  for (const e of edges) {
    if (e.type === "owns") continue;
    const aa = areaOfId.get(e.src);
    const bb = areaOfId.get(e.dst);
    if (!aa || !bb || aa === bb) continue;
    const key = pairKey(aa, bb);
    weights.set(key, (weights.get(key) ?? 0) + 1);
  }
  const raw = relax(packs, weights);
  const fitted = fit(raw, packs.map((p) => p.r));
  const hulls: Hull[] = packs.map((p, i) => ({
    id: p.id,
    cx: fitted.pos[i].x,
    cy: fitted.pos[i].y,
    r: p.r * fitted.scale,
    color: p.color,
    count: p.members.length,
    label: areas.areas[i]?.caption ?? p.id,
    flat: areas.areas[i]?.flat,
  }));
  const hullById = new Map(hulls.map((h) => [h.id, h]));

  const ids: string[] = [];
  const kinds: string[] = [];
  const colors: string[] = [];
  const areaIds: string[] = [];
  const xs: number[] = [];
  const ys: number[] = [];
  const rs: number[] = [];
  packs.forEach((p, i) => {
    const ox = fitted.pos[i].x;
    const oy = fitted.pos[i].y;
    const s = fitted.scale;
    for (const m of p.members) {
      ids.push(m.id);
      kinds.push(m.kind);
      colors.push(p.color);
      areaIds.push(p.id);
      xs.push(ox + m.lx * s);
      ys.push(oy + m.ly * s);
      // Positions are multiplied by the frame scale. Radii live in that same
      // space: leaving them at the pre-fit size collapses every ring once the
      // scale drops below 1 (sparse: most nodes then overlap a neighbour).
      rs.push(m.r * s);
    }
  });

  const grid: Record<string, number[]> = {};
  for (let i = 0; i < ids.length; i++) {
    const key = `${Math.floor(xs[i] / GRID_CELL)},${Math.floor(ys[i] / GRID_CELL)}`;
    const bucket = grid[key];
    if (bucket) bucket.push(i);
    else grid[key] = [i];
  }

  const nonOwns = edges.filter((e) => e.type !== "owns");
  const ranked: RankedEdge[] = nonOwns.map((e, index) => ({
    src: e.src,
    dst: e.dst,
    type: e.type,
    index,
    srcArea: areaOfId.get(e.src) ?? "",
    dstArea: areaOfId.get(e.dst) ?? "",
  }));

  const bundleMap = new Map<string, { a: string; b: string; ab: number; ba: number }>();
  for (const e of ranked) {
    if (!e.srcArea || !e.dstArea || e.srcArea === e.dstArea) continue;
    const a = e.srcArea < e.dstArea ? e.srcArea : e.dstArea;
    const b = e.srcArea < e.dstArea ? e.dstArea : e.srcArea;
    const key = `${a}\0${b}`;
    let row = bundleMap.get(key);
    if (!row) {
      row = { a, b, ab: 0, ba: 0 };
      bundleMap.set(key, row);
    }
    if (e.srcArea === a) row.ab += 1;
    else row.ba += 1;
  }
  const bundles: Bundle[] = [...bundleMap.values()]
    .sort((p, q) => byText(p.a, q.a) || byText(p.b, q.b))
    .map((row) => {
      const ha = hullById.get(row.a)!;
      const hb = hullById.get(row.b)!;
      return {
        ...row,
        colorA: ha.color,
        colorB: hb.color,
        label: bundleLabel(ha.label, hb.label, row.ab, row.ba),
        hullA: ha,
        hullB: hb,
      };
    });

  return {
    ids,
    x: Float32Array.from(xs),
    y: Float32Array.from(ys),
    r: Float32Array.from(rs),
    kinds,
    colors,
    areaOf: areaIds,
    areas: hulls,
    bundles,
    edges: ranked,
    grid,
    depth: areas.depth,
    nodeCount: ids.length,
  };
}

export function bundleLabel(a: string, b: string, ab: number, ba: number): string {
  return `${a}→${b} ${ab} · ${b}→${a} ${ba}`;
}

let lastHitVisits = 0;

/** How many node records the last hit-test inspected. A full scan would equal `ids.length`. */
export function hitVisits(): number {
  return lastHitVisits;
}

/** Added to a node's radius when deciding a hit. Matches the disc the search covers. */
const HIT_SLACK = 1.5;

/**
 * How far a stored radius can reach and still be found inside one 4×4 block.
 * `nodeRadius` tops out at 12.6 for a module; plus slack that is 14.1, and a
 * disc of that radius touches at most four 10-unit cells on a side.
 */
function hitReach(): number {
  return nodeRadius("module", Number.POSITIVE_INFINITY) + HIT_SLACK;
}

/**
 * Spatial hit-test. Looks at one 4×4 block of 10-unit cells — the block the
 * query disc actually covers, not a window pinned to the left of the point —
 * and nothing else. Returns the nearest node whose disc contains the point,
 * or null.
 */
export function hitTest(layout: ClusterLayout, wx: number, wy: number): string | null {
  const reach = hitReach();
  const x0 = Math.floor((wx - reach) / GRID_CELL);
  const y0 = Math.floor((wy - reach) / GRID_CELL);
  const x1 = Math.floor((wx + reach) / GRID_CELL);
  const y1 = Math.floor((wy + reach) / GRID_CELL);
  let best: { id: string; d: number } | null = null;
  let visits = 0;
  for (let dx = 0; dx < HIT_WINDOW && x0 + dx <= x1; dx++) {
    for (let dy = 0; dy < HIT_WINDOW && y0 + dy <= y1; dy++) {
      const bucket = layout.grid[`${x0 + dx},${y0 + dy}`];
      if (!bucket) continue;
      for (const i of bucket) {
        visits += 1;
        const d = Math.hypot(wx - layout.x[i], wy - layout.y[i]);
        if (d <= layout.r[i] + HIT_SLACK && (best === null || d < best.d)) {
          best = { id: layout.ids[i], d };
        }
      }
    }
  }
  lastHitVisits = visits;
  return best?.id ?? null;
}

function gridKey(x: number, y: number): string {
  return `${Math.floor(x / GRID_CELL)},${Math.floor(y / GRID_CELL)}`;
}

/**
 * Lay position overrides on a cached layout.
 *
 * The cached arrays stay as the worker left them. The copy's x/y move, and each
 * moved node leaves its old grid cell for the new one, so hit-testing follows
 * the pin instead of the packed slot. No pins returns the same object.
 */
export function applyPinOverrides(
  layout: ClusterLayout,
  pins: Record<string, { x: number; y: number }>,
): ClusterLayout {
  const entries = Object.entries(pins);
  if (entries.length === 0) return layout;
  const index = new Map(layout.ids.map((id, i) => [id, i]));
  const moves: { i: number; x: number; y: number }[] = [];
  for (const [id, pin] of entries) {
    const i = index.get(id);
    if (i === undefined) continue;
    if (layout.x[i] === pin.x && layout.y[i] === pin.y) continue;
    moves.push({ i, x: pin.x, y: pin.y });
  }
  if (moves.length === 0) return layout;
  const x = new Float32Array(layout.x);
  const y = new Float32Array(layout.y);
  const grid: Record<string, number[]> = {};
  for (const [key, bucket] of Object.entries(layout.grid)) grid[key] = bucket.slice();
  for (const move of moves) {
    const oldKey = gridKey(layout.x[move.i], layout.y[move.i]);
    const bucket = grid[oldKey];
    if (bucket) {
      const at = bucket.indexOf(move.i);
      if (at >= 0) bucket.splice(at, 1);
      if (bucket.length === 0) delete grid[oldKey];
    }
    x[move.i] = move.x;
    y[move.i] = move.y;
    const nextKey = gridKey(move.x, move.y);
    const next = grid[nextKey];
    if (next) next.push(move.i);
    else grid[nextKey] = [move.i];
  }
  return { ...layout, x, y, grid };
}

/**
 * Hulls plus every pinned node. Fit on hulls alone leaves a pin that was
 * dragged outside its area off the frame.
 */
export function fitTargets(
  layout: ClusterLayout,
  pins: Record<string, { x: number; y: number }>,
): { x: number; y: number; r: number }[] {
  const pts = layout.areas.map((a) => ({ x: a.cx, y: a.cy, r: a.r }));
  const index = new Map(layout.ids.map((id, i) => [id, i]));
  for (const [id, pin] of Object.entries(pins)) {
    const i = index.get(id);
    if (i === undefined) continue;
    pts.push({ x: pin.x, y: pin.y, r: layout.r[i] });
  }
  return pts;
}
