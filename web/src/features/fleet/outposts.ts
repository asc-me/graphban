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
}

export interface Outpost {
  host: string;
  specified: boolean;
  online: number;
  agents: OutpostAgent[];
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
      bucket = { host: key, specified: host !== "", online: 0, agents: [] };
      buckets.set(key, bucket);
    }
    const row = outpostAgent(agent);
    bucket.agents.push(row);
    if (row.state !== "offline") bucket.online += 1;
  }
  const list = [...buckets.values()];
  for (const bucket of list) {
    bucket.agents.sort((a, b) => Number(a.state === "offline") - Number(b.state === "offline"));
  }
  list.sort((a, b) => {
    if (a.specified !== b.specified) return a.specified ? -1 : 1;
    return a.host.localeCompare(b.host);
  });
  return list;
}
