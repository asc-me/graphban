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
  type ScaleArea,
  type ScaleEdge,
  type ScaleNode,
} from "./areas";
import { buildAreaGraph, deriveLayers } from "./layers";

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

export type AreaLayoutKind = "clusters" | "map" | "orbit";

/** Map tile, in the same world space as node positions. */
export interface TileRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** One Orbit ring. Index 0 is the core. */
export interface OrbitRing {
  index: number;
  label: string;
  cx: number;
  cy: number;
  r: number;
  areaIds: string[];
}

export interface Hull {
  id: string;
  cx: number;
  cy: number;
  r: number;
  color: string;
  count: number;
  label: string;
  flat?: boolean;
  /** Set by Map. Circle layouts leave it unset, and bundle curves stay radial. */
  rect?: TileRect;
}

/** Unfocused Orbit draws at most this many area-pair bundles. */
export const ORBIT_BUNDLE_CAP = 30;
/** Phyllotaxis step. Irrational, so a shuffled member list cannot pile points up. */
export const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

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
  /** Which canvas layout to compute. Absent means clusters. */
  layout?: AreaLayoutKind;
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
  /** Absent on a hand-built fixture means clusters. */
  layout?: AreaLayoutKind;
  /** Orbit only. Clusters and Map leave this empty. */
  orbitRings?: OrbitRing[];
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
): { pos: { x: number; y: number }[]; scale: number; cx: number; cy: number } {
  if (pos.length === 0) return { pos, scale: 1, cx: 0, cy: 0 };
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
    cx,
    cy,
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

  return finishPlaced({
    layout: "clusters",
    ids,
    xs,
    ys,
    rs,
    kinds,
    colors,
    areaIds,
    hulls,
    depth: areas.depth,
    edges,
    areaOfId,
  });
}

interface PlacedLayout {
  layout: AreaLayoutKind;
  ids: string[];
  xs: number[];
  ys: number[];
  rs: number[];
  kinds: string[];
  colors: string[];
  areaIds: string[];
  hulls: Hull[];
  depth: number | null;
  edges: ScaleEdge[];
  areaOfId: Map<string, string>;
  orbitRings?: OrbitRing[];
}

function finishPlaced(placed: PlacedLayout): ClusterLayout {
  const grid: Record<string, number[]> = {};
  for (let i = 0; i < placed.ids.length; i++) {
    const key = `${Math.floor(placed.xs[i] / GRID_CELL)},${Math.floor(placed.ys[i] / GRID_CELL)}`;
    const bucket = grid[key];
    if (bucket) bucket.push(i);
    else grid[key] = [i];
  }
  const hullById = new Map(placed.hulls.map((h) => [h.id, h]));
  const nonOwns = placed.edges.filter((e) => e.type !== "owns");
  const ranked: RankedEdge[] = nonOwns.map((e, index) => ({
    src: e.src,
    dst: e.dst,
    type: e.type,
    index,
    srcArea: placed.areaOfId.get(e.src) ?? "",
    dstArea: placed.areaOfId.get(e.dst) ?? "",
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
    .flatMap((row) => {
      const ha = hullById.get(row.a);
      const hb = hullById.get(row.b);
      if (!ha || !hb) return [];
      return [{
        ...row,
        colorA: ha.color,
        colorB: hb.color,
        label: bundleLabel(ha.label, hb.label, row.ab, row.ba),
        hullA: ha,
        hullB: hb,
      }];
    });
  return {
    ids: placed.ids,
    x: Float32Array.from(placed.xs),
    y: Float32Array.from(placed.ys),
    r: Float32Array.from(placed.rs),
    kinds: placed.kinds,
    colors: placed.colors,
    areaOf: placed.areaIds,
    areas: placed.hulls,
    bundles,
    edges: ranked,
    grid,
    depth: placed.depth,
    nodeCount: placed.ids.length,
    layout: placed.layout,
    orbitRings: placed.orbitRings ?? [],
  };
}

interface Prepared {
  edges: ScaleEdge[];
  areas: ScaleArea[];
  depth: number | null;
  kindOf: Map<string, string>;
  degree: Map<string, number>;
  areaOfId: Map<string, string>;
}

function prepare(input: ClusterInput): Prepared {
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
  return { edges, areas: areas.areas, depth: areas.depth, kindOf, degree, areaOfId };
}

function orderedMembers(
  memberIds: string[],
  kindOf: Map<string, string>,
  degree: Map<string, number>,
): { id: string; kind: string; degree: number }[] {
  const members = memberIds.map((id) => ({
    id,
    kind: kindOf.get(id) ?? "file",
    degree: degree.get(id) ?? 0,
  }));
  members.sort((a, b) => {
    const ka = KIND_RANK[a.kind] ?? 9;
    const kb = KIND_RANK[b.kind] ?? 9;
    if (ka !== kb) return ka - kb;
    if (a.degree !== b.degree) return b.degree - a.degree;
    return byText(a.id, b.id);
  });
  return members;
}

interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface SqItem {
  id: string;
  area: number;
}

/** Worst aspect ratio of a row laid along `side`. Lower is squarer. */
function worstRatio(row: SqItem[], side: number): number {
  if (row.length === 0 || side <= 0) return Infinity;
  let sum = 0;
  let min = Infinity;
  let max = 0;
  for (const r of row) {
    sum += r.area;
    if (r.area < min) min = r.area;
    if (r.area > max) max = r.area;
  }
  if (min <= 0 || sum <= 0) return Infinity;
  const s2 = sum * sum;
  const w2 = side * side;
  return Math.max((w2 * max) / s2, s2 / (w2 * min));
}

function placeRow(row: SqItem[], box: Box): { rects: { id: string; rect: Box }[]; rest: Box } {
  const sum = row.reduce((s, r) => s + r.area, 0);
  if (box.w >= box.h) {
    const width = box.h <= 0 ? box.w : sum / box.h;
    const rects: { id: string; rect: Box }[] = [];
    let y = box.y;
    for (const r of row) {
      const height = width <= 0 ? 0 : r.area / width;
      rects.push({ id: r.id, rect: { x: box.x, y, w: width, h: height } });
      y += height;
    }
    return {
      rects,
      rest: { x: box.x + width, y: box.y, w: Math.max(0, box.w - width), h: box.h },
    };
  }
  const height = box.w <= 0 ? box.h : sum / box.w;
  const rects: { id: string; rect: Box }[] = [];
  let x = box.x;
  for (const r of row) {
    const width = height <= 0 ? 0 : r.area / height;
    rects.push({ id: r.id, rect: { x, y: box.y, w: width, h: height } });
    x += width;
  }
  return {
    rects,
    rest: { x: box.x, y: box.y + height, w: box.w, h: Math.max(0, box.h - height) },
  };
}

/** Squarified treemap (Bruls). Input order is the placement order. */
function squarify(items: SqItem[], box: Box): { id: string; rect: Box }[] {
  if (items.length === 0 || box.w <= 0 || box.h <= 0) return [];
  if (items.length === 1) {
    return [{ id: items[0].id, rect: { x: box.x, y: box.y, w: box.w, h: box.h } }];
  }
  const side = Math.min(box.w, box.h);
  const row: SqItem[] = [items[0]];
  let i = 1;
  while (i < items.length && worstRatio(row, side) >= worstRatio([...row, items[i]], side)) {
    row.push(items[i]);
    i += 1;
  }
  const placed = placeRow(row, box);
  return placed.rects.concat(squarify(items.slice(i), placed.rest));
}

function insetRect(rect: Box, gap: number): Box {
  const g = Math.min(gap, rect.w / 4, rect.h / 4);
  return {
    x: rect.x + g,
    y: rect.y + g,
    w: Math.max(1, rect.w - g * 2),
    h: Math.max(1, rect.h - g * 2),
  };
}

/**
 * The heavy layouts, dispatched by name. The worker and the inline fallback
 * both call this, so a layout chip cannot compute a different picture from
 * the one the cache stored.
 */
export function layoutFor(kind: AreaLayoutKind | undefined, input: ClusterInput): ClusterLayout {
  if (kind === "map") return mapLayout(input);
  if (kind === "orbit") return orbitLayout(input);
  return clustersLayout(input);
}

/** Heaviest bundles first. Ties break on area id so a shuffle cannot change the cut. */
export function topBundles(bundles: Bundle[], cap: number): Bundle[] {
  if (bundles.length <= cap) return bundles.slice();
  return bundles
    .slice()
    .sort((p, q) => (q.ab + q.ba) - (p.ab + p.ba) || byText(p.a, q.a) || byText(p.b, q.b))
    .slice(0, cap);
}

export function mapLayout(input: ClusterInput): ClusterLayout {
  layoutRuns += 1;
  const prep = prepare(input);
  const margin = 24;
  const bounds: Box = { x: margin, y: margin, w: WORLD_W - margin * 2, h: WORLD_H - margin * 2 };
  const total = prep.areas.reduce((s, a) => s + a.count, 0) || 1;
  const items: SqItem[] = prep.areas
    .map((a) => ({ id: a.id, count: a.count }))
    .sort((a, b) => b.count - a.count || byText(a.id, b.id))
    .map((a) => ({ id: a.id, area: (a.count / total) * bounds.w * bounds.h }));
  const tiles = new Map(squarify(items, bounds).map((t) => [t.id, insetRect(t.rect, 4)]));
  const byId = new Map(prep.areas.map((a) => [a.id, a]));
  const ids: string[] = [];
  const xs: number[] = [];
  const ys: number[] = [];
  const rs: number[] = [];
  const kinds: string[] = [];
  const colors: string[] = [];
  const areaIds: string[] = [];
  const hulls: Hull[] = [];
  for (const item of items) {
    const area = byId.get(item.id);
    const rect = tiles.get(item.id);
    if (!area || !rect) continue;
    const members = orderedMembers(area.members, prep.kindOf, prep.degree);
    const n = Math.max(members.length, 1);
    // Build.dc.html: a header band for the tile's name (34 on a tile taller than 70,
    // else 10), 6 in from the sides and the bottom, and square cells. Filling the whole
    // tile put the first row of nodes under the name.
    const top = rect.h > 70 ? 34 : 10;
    const gx = rect.x + 6;
    const gy = rect.y + top;
    const gw = Math.max(1, rect.w - 12);
    const gh = Math.max(1, rect.h - top - 6);
    let cols = Math.max(1, Math.ceil(Math.sqrt((n * gw) / gh)));
    const cell = Math.min(gw / cols, gh / Math.ceil(n / cols));
    cols = Math.max(1, Math.floor(gw / cell));
    const cellW = cell;
    const cellH = cell;
    members.forEach((m, i) => {
      const col = i % cols;
      const row = Math.floor(i / cols);
      const rad = Math.min(nodeRadius(m.kind, m.degree), cellW / 2 - 0.5, cellH / 2 - 0.5);
      ids.push(m.id);
      kinds.push(m.kind);
      colors.push(area.color);
      areaIds.push(area.id);
      xs.push(gx + (col + 0.5) * cellW);
      ys.push(gy + (row + 0.5) * cellH);
      rs.push(Number.isFinite(rad) ? Math.max(1.25, rad) : 1.25);
    });
    hulls.push({
      id: area.id,
      cx: rect.x + rect.w / 2,
      cy: rect.y + rect.h / 2,
      r: Math.hypot(rect.w, rect.h) / 2,
      color: area.color,
      count: area.count,
      label: area.caption ?? area.id,
      flat: area.flat,
      rect,
    });
  }
  return finishPlaced({
    layout: "map",
    ids,
    xs,
    ys,
    rs,
    kinds,
    colors,
    areaIds,
    hulls,
    depth: prep.depth,
    edges: prep.edges,
    areaOfId: prep.areaOfId,
  });
}

const SPIRAL_C = 16;

function splitEven<T>(items: T[], slots: number): T[][] {
  if (items.length === 0 || slots <= 0) return [];
  const k = Math.min(slots, items.length);
  const base = Math.floor(items.length / k);
  let extra = items.length % k;
  const out: T[][] = [];
  let at = 0;
  for (let s = 0; s < k; s++) {
    const size = base + (extra > 0 ? 1 : 0);
    if (extra > 0) extra -= 1;
    out.push(items.slice(at, at + size));
    at += size;
  }
  return out;
}

function largestId(ids: string[], countOf: Map<string, number>): string {
  let best = ids[0] ?? "";
  let bestN = -1;
  for (const id of [...ids].sort(byText)) {
    const n = countOf.get(id) ?? 0;
    if (n > bestN) {
      best = id;
      bestN = n;
    }
  }
  return best;
}

export function orbitLayout(input: ClusterInput): ClusterLayout {
  layoutRuns += 1;
  const prep = prepare(input);
  const graph = buildAreaGraph(
    prep.areas.map((a) => ({ id: a.id, members: a.members })),
    prep.edges,
  );
  const derived = deriveLayers(graph);
  const countOf = new Map(prep.areas.map((a) => [a.id, a.count]));
  const caption = new Map(prep.areas.map((a) => [a.id, a.caption ?? a.id]));
  const areaById = new Map(prep.areas.map((a) => [a.id, a]));
  const byLayer = new Map<number, string[]>();
  const support: string[] = [];
  for (const row of derived.areas) {
    if (row.layer == null) support.push(row.id);
    else {
      const list = byLayer.get(row.layer) ?? [];
      list.push(row.id);
      byLayer.set(row.layer, list);
    }
  }
  const layerNums = [...byLayer.keys()].sort((a, b) => a - b);
  const hasSupport = support.length > 0;
  const slots = hasSupport ? Math.min(4, layerNums.length) : Math.min(5, layerNums.length);
  const buckets = splitEven(layerNums, slots).map((group) => group.flatMap((n) => byLayer.get(n) ?? []));
  if (hasSupport) buckets.push(support);
  const ringsIn = buckets
    .filter((ids) => ids.length > 0)
    .map((ids) => {
      const ordered = [...ids].sort(byText);
      const best = largestId(ordered, countOf);
      const tag = derived.byId[best]?.tag ?? "L0";
      return { ids: ordered, label: `${tag} · ${caption.get(best) ?? best}` };
    });

  const local = new Map<string, { x: number; y: number; pack: LocalPack }>();
  const ringGeom: { label: string; ids: string[]; r: number }[] = [];
  let prevOuter = 0;
  ringsIn.forEach((ring, i) => {
    const packs = ring.ids.map((id) => {
      const area = areaById.get(id);
      return spiralPack(id, area?.members ?? [], prep.kindOf, prep.degree, area?.color ?? areaColor(id));
    });
    const maxR = Math.max(28, ...packs.map((p) => p.r));
    const n = Math.max(packs.length, 1);
    const chord = packs.length <= 1 ? 0 : (maxR + 22) / Math.sin(Math.PI / n);
    const minCenter = i === 0 ? 0 : prevOuter + maxR + 32;
    const ringR = Math.max(minCenter, chord);
    packs.forEach((p, j) => {
      const ang = -Math.PI / 2 + (j / n) * Math.PI * 2;
      local.set(p.id, { x: Math.cos(ang) * ringR, y: Math.sin(ang) * ringR, pack: p });
    });
    prevOuter = ringR + maxR;
    ringGeom.push({ label: ring.label, ids: ring.ids, r: ringR });
  });

  const order = [...local.keys()];
  const raw = order.map((id) => {
    const at = local.get(id)!;
    return { x: at.x, y: at.y };
  });
  const radii = order.map((id) => local.get(id)!.pack.r);
  const fitted = fit(raw, radii);
  const ids: string[] = [];
  const xs: number[] = [];
  const ys: number[] = [];
  const rs: number[] = [];
  const kinds: string[] = [];
  const colors: string[] = [];
  const areaIds: string[] = [];
  const hulls: Hull[] = order.map((id, i) => {
    const pack = local.get(id)!.pack;
    const area = areaById.get(id);
    const ox = fitted.pos[i].x;
    const oy = fitted.pos[i].y;
    const s = fitted.scale;
    for (const m of pack.members) {
      ids.push(m.id);
      kinds.push(m.kind);
      colors.push(pack.color);
      areaIds.push(id);
      xs.push(ox + m.lx * s);
      ys.push(oy + m.ly * s);
      rs.push(m.r * s);
    }
    return {
      id,
      cx: ox,
      cy: oy,
      r: pack.r * s,
      color: pack.color,
      count: pack.members.length,
      label: area?.caption ?? id,
      flat: area?.flat,
    };
  });
  const orbitRings: OrbitRing[] = ringGeom.map((ring, index) => ({
    index,
    label: ring.label,
    areaIds: ring.ids,
    cx: WORLD_W / 2 + (0 - fitted.cx) * fitted.scale,
    cy: WORLD_H / 2 + (0 - fitted.cy) * fitted.scale,
    r: ring.r * fitted.scale,
  }));
  return finishPlaced({
    layout: "orbit",
    ids,
    xs,
    ys,
    rs,
    kinds,
    colors,
    areaIds,
    hulls,
    depth: prep.depth,
    edges: prep.edges,
    areaOfId: prep.areaOfId,
    orbitRings,
  });
}

function spiralPack(
  id: string,
  memberIds: string[],
  kindOf: Map<string, string>,
  degree: Map<string, number>,
  color: string,
): LocalPack {
  const members = orderedMembers(memberIds, kindOf, degree);
  const placed: LocalPack["members"] = members.map((m, i) => {
    const rad = i === 0 ? 0 : SPIRAL_C * Math.sqrt(i);
    const ang = i * GOLDEN_ANGLE;
    return {
      id: m.id,
      kind: m.kind,
      lx: Math.cos(ang) * rad,
      ly: Math.sin(ang) * rad,
      r: nodeRadius(m.kind, m.degree),
    };
  });
  let r = 28;
  for (const m of placed) r = Math.max(r, Math.hypot(m.lx, m.ly) + m.r + 16);
  return { id, kind: "", color, members: placed, r };
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
