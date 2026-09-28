import type { FleetOverview, LiveAgent, LiveBoard, LiveFeedRow, LiveUser } from "@/lib/types";

/** Same mapping as Fleet: role colour is the status that role produces. */
export const ROLE_TONE: Record<string, string> = {
  planner: "text-[#b794f6] border-[#b794f6]/40",
  worker: "text-[color:var(--color-st-in_progress)] border-[color:var(--color-st-in_progress)]/40",
  reviewer: "text-[color:var(--color-st-review)] border-[color:var(--color-st-review)]/40",
  "all-in-one": "text-muted border-line-2",
};

export const PLANNER_TOOLS = new Set([
  "propose_allocation",
  "assign_role",
  "delegate",
  "collision_clusters",
  "claim_cluster",
  "mint_enrolment",
  "retire_wave",
  "fleet_status",
  "claim_next",
  "next_cluster",
]);

export type PlannerVerb = "allocate" | "hold" | "re-task" | "split";

export type TicketDotKind = "read" | "write" | "review" | "reported" | "planner" | "refused";

export type StreamFilter = "all" | "writes" | "reads" | "planner" | "failures";

export const STREAM_FILTERS: StreamFilter[] = ["all", "writes", "reads", "planner", "failures"];

export interface FlatAgent {
  agent: LiveAgent;
  userLabel: string;
}

export interface TicketLane {
  id: string;
  title: string;
  status: string;
  holderId: string | null;
  holderLabel: string | null;
  lastEvent: string | null;
  dots: { kind: TicketDotKind; at: string | null; tool: string }[];
}

/** Age against the payload clock (D6), not the browser clock. */
export function ageLabel(at: string | null, servedAt: string): string {
  if (!at) return "no heartbeat yet";
  const then = Date.parse(/(Z|[+-]\d{2}:?\d{2})$/.test(at) ? at : `${at}Z`);
  const now = Date.parse(/(Z|[+-]\d{2}:?\d{2})$/.test(servedAt) ? servedAt : `${servedAt}Z`);
  if (Number.isNaN(then) || Number.isNaN(now)) return "no heartbeat yet";
  const s = Math.max(0, Math.floor((now - then) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function durationLabel(s: number): string {
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

export function flattenAgents(users: LiveUser[]): FlatAgent[] {
  const out: FlatAgent[] = [];
  for (const u of users) {
    for (const a of u.agents) {
      out.push({ agent: a, userLabel: u.label });
    }
  }
  return out;
}

export function agentActivity(a: LiveAgent, servedAt: string): string {
  if (a.status_state === "reported" && a.status?.text) return a.status.text;
  if (a.call_state === "never" || !a.last_call) return "no calls recorded";
  const age = ageLabel(a.last_call.at, servedAt);
  return `${a.last_call.tool}${a.last_call.target ? ` · ${a.last_call.target}` : ""} · ${age}`;
}

export function plannerVerb(tool: string): PlannerVerb | null {
  if (tool === "delegate" || tool === "assign_role" || tool === "propose_allocation" || tool === "claim_next" || tool === "next_cluster") {
    return "allocate";
  }
  if (tool === "release_item") return "hold";
  if (tool === "bounce") return "re-task";
  if (tool === "decompose_prd") return "split";
  return null;
}

export function dotKind(row: LiveFeedRow): TicketDotKind {
  if (row.source === "reported") return "reported";
  if (!row.ok) return "refused";
  if (PLANNER_TOOLS.has(row.tool)) return "planner";
  if (row.tool === "sign_off" || row.tool === "bounce" || row.tool === "claim_review") return "review";
  if (row.write) return "write";
  return "read";
}

const DOT_COLOUR: Record<TicketDotKind, string> = {
  read: "bg-muted",
  write: "bg-st-in_progress",
  review: "bg-st-review",
  reported: "bg-surface-3 border border-line-2",
  planner: "bg-[#b794f6]",
  refused: "bg-st-blocked",
};

export function dotClass(kind: TicketDotKind): string {
  return DOT_COLOUR[kind] ?? "bg-faint";
}

export function matchesStreamFilter(r: LiveFeedRow, f: StreamFilter): boolean {
  if (f === "all") return true;
  if (f === "failures") return !r.ok;
  if (f === "planner") return PLANNER_TOOLS.has(r.tool);
  if (r.source === "reported") return f === "writes";
  return f === "writes" ? r.write : !r.write;
}

export interface LiveKpis {
  online: number;
  touchesPerMinute: number | null;
  plannerCalls: number;
  refused: number;
}

export function computeKpis(board: LiveBoard, flat: FlatAgent[]): LiveKpis {
  const online = flat.filter(({ agent }) => agent.state !== "offline").length;
  const windowMin = board.window_seconds > 0 ? board.window_seconds / 60 : null;
  const touches = flat.reduce((n, { agent }) => n + (agent.calls_in_window || 0), 0);
  const touchesPerMinute = windowMin ? Math.round((touches / windowMin) * 10) / 10 : null;
  const plannerCalls = flat.reduce((n, { agent }) => {
    if (agent.role !== "planner" && agent.role !== "all-in-one") return n;
    return n + (agent.calls_in_window || 0);
  }, 0);
  const refused = flat.filter(({ agent }) => agent.last_call && !agent.last_call.ok).length;
  return { online, touchesPerMinute, plannerCalls, refused };
}

const LANE_WINDOW_MS = 10 * 60 * 1000;

export function buildTicketLanes(flat: FlatAgent[], servedAt: string, feedRows: LiveFeedRow[]): TicketLane[] {
  const now = Date.parse(/(Z|[+-]\d{2}:?\d{2})$/.test(servedAt) ? servedAt : `${servedAt}Z`);
  const cutoff = now - LANE_WINDOW_MS;
  const byItem = new Map<string, TicketLane>();

  for (const { agent } of flat) {
    for (const h of agent.holdings) {
      if (!h.id) continue;
      const existing = byItem.get(h.id);
      if (!existing) {
        byItem.set(h.id, {
          id: h.id,
          title: h.title,
          status: h.status,
          holderId: agent.id,
          holderLabel: agent.label || agent.id,
          lastEvent: null,
          dots: [],
        });
      }
    }
  }

  for (const row of feedRows) {
    const target = row.target?.split(/\s+/)[0] ?? "";
    if (!target || !byItem.has(target)) continue;
    const atMs = row.at ? Date.parse(/(Z|[+-]\d{2}:?\d{2})$/.test(row.at) ? row.at : `${row.at}Z`) : NaN;
    if (Number.isNaN(atMs) || atMs < cutoff) continue;
    const lane = byItem.get(target)!;
    lane.dots.push({ kind: dotKind(row), at: row.at, tool: row.tool });
    lane.lastEvent = `${row.tool}${row.target ? ` · ${row.target}` : ""}`;
  }

  for (const { agent } of flat) {
    const call = agent.last_call;
    if (!call?.at || !call.target) continue;
    const itemId = call.target.split(/\s+/)[0];
    const lane = byItem.get(itemId);
    if (!lane) continue;
    const atMs = Date.parse(/(Z|[+-]\d{2}:?\d{2})$/.test(call.at) ? call.at : `${call.at}Z`);
    if (Number.isNaN(atMs) || atMs < cutoff) continue;
    if (!lane.lastEvent) {
      lane.lastEvent = `${call.tool} · ${call.target}`;
    }
    if (lane.dots.length === 0) {
      lane.dots.push({
        kind: call.ok ? (PLANNER_TOOLS.has(call.tool) ? "planner" : call.tool.includes("sign") ? "review" : "read") : "refused",
        at: call.at,
        tool: call.tool,
      });
    }
  }

  return [...byItem.values()].sort((a, b) => a.id.localeCompare(b.id));
}

export function heldBackClusters(fleet: FleetOverview | undefined) {
  return (fleet?.clusters ?? []).filter((c) => c.held_by);
}

export interface FeedRun { row: LiveFeedRow; count: number; last: LiveFeedRow }

export function collapseRuns(rows: LiveFeedRow[]): FeedRun[] {
  const out: FeedRun[] = [];
  for (const r of rows) {
    const prev = out[out.length - 1];
    if (
      prev
      && r.source === "observed"
      && prev.row.source === "observed"
      && prev.row.tool === r.tool
      && prev.row.target === r.target
      && prev.row.ok === r.ok
    ) {
      prev.count += 1;
      prev.last = r;
      continue;
    }
    out.push({ row: r, count: 1, last: r });
  }
  return out;
}
