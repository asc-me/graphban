/**
 * Presence clouds, the bottom layer (PRD-52 D5 z-order, D6).
 *
 * Zoomed out: one cloud per (user, area). At fine zoom (the same 2.6× that
 * opens individual edges): one cloud per held node. `cloudsFor` does the
 * geometry; this module only chooses which nodes it sees.
 *
 * A predicted area keeps a dashed edge. Solo fades every other user's cloud
 * to the same 0.04 the SVG view uses, and leaves the soloed user at 0.16.
 */

import type { FleetPresence } from "@/lib/types";
import type { Pos } from "@/lib/graph/layout";
import { cloudsFor, indexByNode, type Cloud as PresenceCloud } from "@/lib/graph/presence";
import { LOD_ZOOM } from "./edges";
import type { Stroke } from "./edges";
import { applyCamera } from "./nodes";
import type { DrawScene } from "./scene";

/** Soft fill. Matches the SVG cloud's 0.16, and the faded 0.04 under solo. */
export const CLOUD_ALPHA = 0.16;
export const CLOUD_SOLO_ALPHA = 0.04;
/** Mean of the 0.85 / 0.3 hold pulse. Reduced motion holds the ring here. */
export const PULSE_MEAN = 0.575;
export const CONTENTION_COLOR = "#ff6b6b";

export interface Cloud {
  x: number;
  y: number;
  r: number;
  color: string;
  userId: string;
  predicted: boolean;
  alpha: number;
}

export interface CloudLayout {
  ids: readonly string[];
  x: ArrayLike<number>;
  y: ArrayLike<number>;
  areaOf: readonly string[];
}

function presenceOn(presence: FleetPresence, paths: ReadonlySet<string>): FleetPresence {
  const held = [];
  for (const row of presence.held) {
    const node_paths = (row.node_paths ?? []).filter((p) => paths.has(p));
    if (node_paths.length) held.push({ ...row, node_paths });
  }
  return { ...presence, held };
}

function paint(cloud: PresenceCloud, soloUser: string | null): Cloud {
  const faded = !!soloUser && cloud.userId !== soloUser;
  return {
    x: cloud.cx,
    y: cloud.cy,
    r: cloud.r,
    color: cloud.color,
    userId: cloud.userId,
    predicted: cloud.predicted,
    alpha: faded ? CLOUD_SOLO_ALPHA : CLOUD_ALPHA,
  };
}

/** Several proximity blobs in one area become the one cloud that area owes. */
function enclose(group: PresenceCloud[]): PresenceCloud {
  if (group.length === 1) return group[0];
  const weight = group.reduce((s, g) => s + g.count, 0) || group.length;
  const cx = group.reduce((s, g) => s + g.cx * g.count, 0) / weight;
  const cy = group.reduce((s, g) => s + g.cy * g.count, 0) / weight;
  const r = Math.max(...group.map((g) => Math.hypot(g.cx - cx, g.cy - cy) + g.r));
  return {
    ...group[0],
    cx,
    cy,
    r,
    count: weight,
    predicted: group.some((g) => g.predicted),
  };
}

/**
 * Clouds for one frame. `cloudsFor` is called unchanged on the nodes this
 * zoom is willing to show: an area's members together, or one node alone.
 */
export function canvasClouds(
  layout: CloudLayout,
  presence: FleetPresence | null | undefined,
  zoom: number,
  soloUser: string | null = null,
): Cloud[] {
  if (!presence?.held.length) return [];
  const at = new Map<string, number>();
  for (let i = 0; i < layout.ids.length; i++) at.set(layout.ids[i], i);
  const fine = zoom >= LOD_ZOOM;
  const out: Cloud[] = [];

  if (fine) {
    for (const path of indexByNode(presence).keys()) {
      const i = at.get(path);
      if (i === undefined) continue;
      const pos: Record<string, Pos> = { [path]: { x: layout.x[i], y: layout.y[i] } };
      for (const cloud of cloudsFor(presenceOn(presence, new Set([path])), pos)) {
        out.push(paint(cloud, soloUser));
      }
    }
  } else {
    const byArea = new Map<string, Set<string>>();
    for (const row of presence.held) {
      for (const path of row.node_paths ?? []) {
        const i = at.get(path);
        if (i === undefined) continue;
        const area = layout.areaOf[i] || "other";
        const set = byArea.get(area);
        if (set) set.add(path);
        else byArea.set(area, new Set([path]));
      }
    }
    for (const paths of byArea.values()) {
      const pos: Record<string, Pos> = {};
      for (const path of paths) {
        const i = at.get(path)!;
        pos[path] = { x: layout.x[i], y: layout.y[i] };
      }
      const parts = cloudsFor(presenceOn(presence, paths), pos);
      const byUser = new Map<string, PresenceCloud[]>();
      for (const cloud of parts) {
        const list = byUser.get(cloud.userId);
        if (list) list.push(cloud);
        else byUser.set(cloud.userId, [cloud]);
      }
      for (const group of byUser.values()) out.push(paint(enclose(group), soloUser));
    }
  }

  // The same node can arrive on two rows. One disc per (user, place).
  const seen = new Set<string>();
  const unique: Cloud[] = [];
  for (const cloud of out) {
    const key = `${cloud.userId}@${cloud.x.toFixed(2)},${cloud.y.toFixed(2)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(cloud);
  }
  unique.sort((a, b) => a.userId.localeCompare(b.userId) || a.x - b.x || a.y - b.y);
  return unique;
}

/** Bottom layer for both renderers. An empty list is "no presence", not a skipped layer. */
export function presenceClouds(scene: DrawScene): readonly Cloud[] {
  return scene.clouds ?? [];
}

function rgba(color: string, alpha: number): string {
  let h = color.trim();
  if (h.startsWith("#")) h = h.slice(1);
  if (h.length === 3) h = `${h[0]}${h[0]}${h[1]}${h[1]}${h[2]}${h[2]}`;
  const n = Number.parseInt(h.slice(0, 6), 16);
  if (!Number.isFinite(n) || h.length < 6) return `rgba(139,148,158,${alpha})`;
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

/**
 * Dashed rim for a predicted area, in world units. Both renderers stroke
 * these segments, so the dash is the same pixels and not a Canvas-only effect.
 */
export function predictedStrokes(clouds: readonly Cloud[]): Stroke[] {
  const out: Stroke[] = [];
  const dash = 6;
  const gap = 5;
  const period = dash + gap;
  for (const cloud of clouds) {
    if (!cloud.predicted || cloud.r <= 0) continue;
    const circ = 2 * Math.PI * cloud.r;
    const n = Math.max(1, Math.round(circ / period));
    for (let i = 0; i < n; i++) {
      const a0 = ((i * period) / circ) * Math.PI * 2;
      const a1 = ((i * period + dash) / circ) * Math.PI * 2;
      out.push({
        kind: "line",
        x0: cloud.x + Math.cos(a0) * cloud.r,
        y0: cloud.y + Math.sin(a0) * cloud.r,
        cx: 0,
        cy: 0,
        x1: cloud.x + Math.cos(a1) * cloud.r,
        y1: cloud.y + Math.sin(a1) * cloud.r,
        color: cloud.color,
        widthPx: 1.5,
        label: "",
      });
    }
  }
  return out;
}

export function drawClouds(ctx: CanvasRenderingContext2D, scene: DrawScene): void {
  const clouds = presenceClouds(scene);
  if (clouds.length === 0) return;
  applyCamera(ctx, scene);
  const k = Math.max(scene.camera.k, 0.05);
  for (const cloud of clouds) {
    const fill = ctx.createRadialGradient(cloud.x, cloud.y, 0, cloud.x, cloud.y, cloud.r);
    fill.addColorStop(0, rgba(cloud.color, cloud.alpha));
    fill.addColorStop(1, rgba(cloud.color, 0));
    ctx.beginPath();
    ctx.fillStyle = fill;
    ctx.arc(cloud.x, cloud.y, cloud.r, 0, Math.PI * 2);
    ctx.fill();
  }
  const dashes = predictedStrokes(clouds);
  if (dashes.length === 0) return;
  ctx.globalAlpha = 0.45;
  ctx.lineWidth = 1.5 / k;
  ctx.setLineDash([]);
  for (const stroke of dashes) {
    ctx.beginPath();
    ctx.strokeStyle = stroke.color;
    ctx.moveTo(stroke.x0, stroke.y0);
    ctx.lineTo(stroke.x1, stroke.y1);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
}
