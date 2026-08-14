import type { FleetPresence, HeldArea } from "@/lib/types";

import type { Pos } from "./layout";

/**
 * Role colours as hex, for SVG.
 *
 * `FleetView`'s `ROLE_TONE` is Tailwind classes and cannot be used in an SVG `fill`, so the
 * values are mirrored here against the same tokens. `all-in-one` is deliberately NOT one of the
 * three role colours, for the reason FleetView already gives: it is not a worker that also
 * reviews, it is the other posture, and tinting it as a role would say the opposite.
 */
export const ROLE_HEX: Record<string, string> = {
  planner: "#b794f6",
  worker: "#c6f24e", // --color-st-in_progress
  reviewer: "#e0b34a", // --color-st-review
  "all-in-one": "#8b949e", // --color-muted
};

export const roleHex = (role: string | null | undefined) =>
  ROLE_HEX[role ?? ""] ?? "#8b949e";

/** Every held area that resolved onto a given node path. */
export function indexByNode(presence: FleetPresence | undefined): Map<string, HeldArea[]> {
  const out = new Map<string, HeldArea[]>();
  for (const row of presence?.held ?? []) {
    for (const path of row.node_paths ?? []) {
      const list = out.get(path);
      if (list) list.push(row);
      else out.set(path, [row]);
    }
  }
  // Sorted per node so a node held by two agents renders them in a stable order.
  for (const list of out.values()) {
    list.sort((a, b) => (a.agent_id ?? "").localeCompare(b.agent_id ?? ""));
  }
  return out;
}

/** Distinct humans holding anything, in a stable order — the fleet legend's rows (D7). */
export function holdersOf(presence: FleetPresence | undefined) {
  const by = new Map<string, { userId: string; initials: string; color: string; nodes: Set<string>; agents: Set<string> }>();
  for (const row of presence?.held ?? []) {
    const id = row.user_id ?? "unknown";
    let entry = by.get(id);
    if (!entry) {
      entry = {
        userId: id,
        initials: row.user_initials || "??",
        color: row.user_color || "#8b949e",
        nodes: new Set(),
        agents: new Set(),
      };
      by.set(id, entry);
    }
    if (row.agent_id) entry.agents.add(row.agent_id);
    for (const p of row.node_paths ?? []) entry.nodes.add(p);
  }
  return [...by.values()].sort((a, b) => a.userId.localeCompare(b.userId));
}

export interface Contention {
  /** How many agents hold this node. Two windows of one person count as two. */
  agents: number;
  /** How many distinct HUMANS. This is what the alarm is keyed on. */
  users: number;
  /** True from two distinct users up. Binary — three users is not "more contended". */
  contended: boolean;
}

/**
 * Is this node held by more than one human? (PRD-20 D6)
 *
 * **Keyed on distinct USERS, not agents**, and that is the whole design. Two agents belonging
 * to one person is ordinary — one human, two windows, one worktree — and ringing it would cry
 * wolf on the normal case until nobody looked at the ring again. Two humans is the case where
 * nobody involved can see the other's terminal, which is the only case worth an alarm.
 *
 * A `null` user_id counts as its own holder rather than collapsing with other nulls: an agent
 * whose human cannot be resolved is exactly the situation where assuming they are all the same
 * person would hide a real collision.
 */
export function contentionOf(holders: HeldArea[]): Contention {
  const users = new Set<string>();
  holders.forEach((h, i) => users.add(h.user_id ?? `unknown:${h.agent_id ?? i}`));
  return { agents: holders.length, users: users.size, contended: users.size > 1 };
}

/** `held by 2 agents across 2 users` — read, not glanced. The ring does the glancing. */
export function describeContention(c: Contention): string {
  if (c.agents === 0) return "";
  const a = `${c.agents} agent${c.agents === 1 ? "" : "s"}`;
  if (c.users <= 1) return `held by ${a}`;
  return `held by ${a} across ${c.users} users`;
}

export interface Cloud {
  userId: string;
  color: string;
  cx: number;
  cy: number;
  r: number;
  /** How many held nodes this blob covers — what its radius is scaled to. */
  count: number;
  /** True when ANY area feeding this blob came from `predict_areas` rather than touchpoints. */
  predicted: boolean;
}

/** Nodes within this distance of each other join one blob rather than getting their own. */
const CLUSTER_RADIUS = 140;
const MIN_R = 34;
const PAD = 26;

/**
 * One blurred blob per (user, nearby group of held nodes) — PRD-20 D5.
 *
 * Not one circle per node: a fleet working an area should read as one region rather than a
 * scatter of dots, which is also the mitigation section 7 names for the cost of this layer at
 * scale. Single-linkage grouping within `CLUSTER_RADIUS`, walked in sorted order so the same
 * inputs always produce the same blobs — the layout is deterministic and a presence overlay
 * that reshuffled on top of it would give that away.
 */
export function cloudsFor(
  presence: FleetPresence | undefined,
  pos: Record<string, Pos>,
): Cloud[] {
  const out: Cloud[] = [];
  for (const holder of holdersOf(presence)) {
    const points = [...holder.nodes]
      .sort()
      .map((path) => ({ path, p: pos[path] }))
      .filter((n): n is { path: string; p: Pos } => !!n.p);
    if (points.length === 0) continue;

    const predictedPaths = new Set<string>();
    for (const row of presence?.held ?? []) {
      if (row.predicted && row.user_id === holder.userId) {
        for (const p of row.node_paths ?? []) predictedPaths.add(p);
      }
    }

    const unassigned = [...points];
    while (unassigned.length) {
      const seed = unassigned.shift()!;
      const group = [seed];
      // Single linkage: keep absorbing anything within reach of anything already in the group.
      let grew = true;
      while (grew) {
        grew = false;
        for (let i = unassigned.length - 1; i >= 0; i--) {
          const cand = unassigned[i];
          if (group.some((g) => Math.hypot(g.p.x - cand.p.x, g.p.y - cand.p.y) <= CLUSTER_RADIUS)) {
            group.push(cand);
            unassigned.splice(i, 1);
            grew = true;
          }
        }
      }
      const cx = group.reduce((s, g) => s + g.p.x, 0) / group.length;
      const cy = group.reduce((s, g) => s + g.p.y, 0) / group.length;
      const spread = Math.max(...group.map((g) => Math.hypot(g.p.x - cx, g.p.y - cy)));
      out.push({
        userId: holder.userId,
        color: holder.color,
        cx,
        cy,
        r: Math.max(MIN_R, spread + PAD),
        count: group.length,
        predicted: group.some((g) => predictedPaths.has(g.path)),
      });
    }
  }
  return out;
}

/**
 * Seconds left on a reservation, measured against the payload's OWN `served_at`.
 *
 * Not against the browser clock: a viewer whose clock is a minute out would otherwise see every
 * lease as a minute short, and presence honesty is the whole point of showing this at all.
 * Negative is clamped to 0 — an expired row should read as expired, never as a countdown that
 * has gone backwards.
 */
export function secondsRemaining(expiresAt: string | null, servedAt: string): number | null {
  if (!expiresAt) return null;
  const end = Date.parse(expiresAt);
  const now = Date.parse(servedAt);
  if (Number.isNaN(end) || Number.isNaN(now)) return null;
  return Math.max(0, Math.round((end - now) / 1000));
}

/** `4m 10s` / `48s` / `expired`. Compact enough for an inspector line. */
export function formatRemaining(seconds: number | null): string {
  if (seconds === null) return "";
  if (seconds <= 0) return "expired";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}
