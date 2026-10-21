import type { FleetAgent } from "@/lib/types";

export interface OutpostAgent {
  id: string;
  key: string;
  label: string;
  state: string;
  vendor: string;
  model: string;
  tier: string;
  os: string;
  worktree: string;
  branch: string;
  /** How this agent connected: "gbfleet" when enrolled in a wave, "gban" otherwise. */
  via: string;
}

export interface Outpost {
  host: string;
  specified: boolean;
  online: number;
  os: string;
  agents: OutpostAgent[];
}

/** Host-level OS: the values agents declared, never localhost-by-default. */
export function hostOs(agents: OutpostAgent[], specified: boolean): string {
  if (!specified) return "no host declared";
  const values = [...new Set(agents.map((a) => a.os).filter(Boolean))];
  if (values.length === 0) return "os not declared";
  if (values.length === 1) return values[0];
  return "mixed OS";
}

export type OutpostHint = { text: string; fixLabel: string; fixTo: "setup" | "fleet" | "" };

/** Hints the drawing can derive from the roster. Version-stale is omitted — we
 *  do not have a server version to compare against (PRD-47 G3). */
export function hostHint(post: Outpost): OutpostHint | null {
  if (!post.specified) {
    return {
      text: "Registered without a host capability or a model @ host label, so it can't be placed.",
      fixLabel: "",
      fixTo: "",
    };
  }
  const offlineHolding = post.agents.filter(
    (a) => a.state === "offline" && (a.branch || a.worktree),
  );
  if (offlineHolding.length > 0) {
    const one = offlineHolding[0];
    const where = one.branch || one.worktree;
    return {
      text: `Offline while holding ${where}. gbfleet reaps only when its wave ends.`,
      fixLabel: "Open on Fleet →",
      fixTo: "fleet",
    };
  }
  const offline = post.agents.filter((a) => a.state === "offline");
  if (offline.length > 0) {
    return {
      text: `${offline.length} agent${offline.length === 1 ? "" : "s"} offline. Check the machine is reachable and the agent process is running.`,
      fixLabel: "",
      fixTo: "",
    };
  }
  return null;
}

function capString(caps: Record<string, unknown> | undefined, key: string): string {
  const value = caps?.[key];
  return typeof value === "string" ? value.trim() : "";
}

/** Host this agent registered from. Empty is unspecified — never localhost-by-default. */
export function outpostHost(agent: FleetAgent): string {
  const fromCaps = capString(agent.capabilities, "host");
  if (fromCaps) return fromCaps;
  const match = /@\s*([^:\s]+)/.exec(agent.label || "");
  return match ? match[1].trim() : "";
}

export function outpostAgent(agent: FleetAgent): OutpostAgent {
  const caps = agent.capabilities || {};
  return {
    id: agent.id,
    key: agent.key,
    label: agent.label,
    state: agent.state,
    vendor: capString(caps, "vendor"),
    model: capString(caps, "model"),
    tier: capString(caps, "tier"),
    os: capString(caps, "os") || capString(caps, "platform"),
    worktree: agent.worktree || "",
    branch: agent.branch || "",
    via: agent.enrolled ? "gbfleet" : "gban",
  };
}

/**
 * Group roster rows into gban/gbfleet hosts (GRPH-866).
 *
 * Live agents first inside a host; unspecified hosts sort last. An empty list means
 * nobody has registered — that is not "zero outposts found and that is fine".
 */
export function groupOutposts(agents: FleetAgent[]): Outpost[] {
  const buckets = new Map<string, Outpost>();
  for (const agent of agents) {
    if (agent.dismissed) continue;
    const host = outpostHost(agent);
    const key = host || "unspecified";
    let bucket = buckets.get(key);
    if (!bucket) {
      bucket = { host: key, specified: host !== "", online: 0, os: "", agents: [] };
      buckets.set(key, bucket);
    }
    const row = outpostAgent(agent);
    bucket.agents.push(row);
    if (row.state !== "offline") bucket.online += 1;
  }
  const list = [...buckets.values()];
  for (const bucket of list) {
    bucket.agents.sort((a, b) => Number(a.state === "offline") - Number(b.state === "offline"));
    bucket.os = hostOs(bucket.agents, bucket.specified);
  }
  list.sort((a, b) => {
    if (a.specified !== b.specified) return a.specified ? -1 : 1;
    return a.host.localeCompare(b.host);
  });
  return list;
}
