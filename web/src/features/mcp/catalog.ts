/**
 * Overlay for the MCP Tools page (PRD-47 S5 / Build.dc.html).
 *
 * `/api/mcp/tools` serves name, description, params, calls, status. The drawing also
 * wants short, when, example, returns, roles, refused-when, usually-followed-by, and
 * tier. Those are frontend-only (PRD-47 D4). Unlisted tools fall back to the live
 * description / a generated call shape; refused/next/returns are omitted rather than
 * invented. Tier is the same map as `backend/app/services/tool_tiers.py` — unlisted
 * is core, never a guessed specialist bucket.
 */
import type { McpParamDetail, McpToolInfo } from "@/lib/types";

export type ToolTier = "core" | "prd" | "codegraph" | "fleet" | "misc";

/** Same membership as `tool_tiers.TOOL_TIERS`. Unlisted → core. */
const TIER_OF: Record<string, Exclude<ToolTier, "core">> = {
  create_prd: "prd", update_prd: "prd", grill_prd: "prd", answer_grill: "prd",
  decompose_prd: "prd", close_prd: "prd", request_rebaseline: "prd", submit_verdict: "prd",
  describe_code: "codegraph", link_code: "codegraph", unlink_code: "codegraph",
  record_scan: "codegraph", scan_status: "codegraph", scans_due: "codegraph",
  propose_allocation: "fleet", assign_role: "fleet", mint_enrolment: "fleet",
  retire_wave: "fleet", delegate: "fleet",
  extract_lessons: "misc", generate_digest: "misc", report_graphban_issue: "misc",
  learning_loop: "misc", create_project: "misc", publish_memory: "misc",
  reject_memory: "misc",
};

export function tierOf(name: string): ToolTier {
  return TIER_OF[name] ?? "core";
}

export const TIER_TONE: Record<ToolTier, { border: string; color: string }> = {
  core: { border: "border-control", color: "text-muted" },
  prd: { border: "border-[#7ca2ff]/40", color: "text-[#7ca2ff]" },
  codegraph: { border: "border-[#a78bfa]/40", color: "text-[#c9b8ff]" },
  fleet: { border: "border-accent/40", color: "text-accent" },
  misc: { border: "border-control", color: "text-faint" },
};

export const GROUPS: { id: string; label: string; hint: string; names: string[] }[] = [
  {
    id: "start",
    label: "Start a session",
    hint: "every agent, every time",
    names: ["get_context", "register_agent", "heartbeat", "list_projects", "setup_project"],
  },
  {
    id: "work",
    label: "Take and do work",
    hint: "workers and all-in-one",
    names: [
      "claim_cluster", "claim_next", "next_cluster", "update_item", "get_item_details",
      "search_items", "get_backlog", "suggest_next", "release_item", "create_item",
    ],
  },
  {
    id: "review",
    label: "Review",
    hint: "never your own",
    names: ["claim_review", "sign_off", "bounce"],
  },
  {
    id: "plan",
    label: "Plan",
    hint: "planners",
    names: [
      "collision_clusters", "propose_allocation", "assign_role", "mint_enrolment",
      "retire_wave", "create_prd", "update_prd", "grill_prd", "answer_grill",
      "decompose_prd", "close_prd", "submit_verdict", "request_rebaseline",
      "get_prd", "prd_coverage", "prd_acceptance", "fleet_status", "delegate",
    ],
  },
  {
    id: "memory",
    label: "Memory",
    hint: "read before, write after",
    names: [
      "search_memory", "add_memory", "get_lessons", "extract_lessons",
      "publish_memory", "reject_memory", "learning_loop", "review_recommendation",
    ],
  },
  {
    id: "code",
    label: "Code graph",
    hint: "describe as you read",
    names: [
      "describe_code", "get_code_map", "search_code", "code_neighbors",
      "graph_query", "link_code", "unlink_code",
      "record_scan", "scan_status", "scans_due",
    ],
  },
  {
    id: "more",
    label: "More",
    hint: "links, reports, projects",
    names: [
      "link_items", "unlink_items", "related_work", "generate_digest",
      "report_graphban_issue", "create_project",
    ],
  },
];

const GROUP_BY_NAME = new Map<string, string>();
for (const g of GROUPS) for (const n of g.names) GROUP_BY_NAME.set(n, g.id);

export function groupIdOf(name: string): string {
  return GROUP_BY_NAME.get(name) ?? "more";
}

/** Same membership as `fleet.TOOL_ROLES`. Unlisted → every posture. */
const PLANNER_ONLY = new Set([
  "retire_wave", "assign_role", "mint_enrolment", "create_prd", "update_prd",
  "decompose_prd", "grill_prd", "answer_grill",
]);
const WORKER_ONLY = new Set([
  "claim_next", "next_cluster", "claim_cluster", "release_item",
  "claim_review", "sign_off", "bounce", "record_scan",
]);

export type RoleId = "all-in-one" | "worker" | "planner";

export function rolesOf(name: string): RoleId[] {
  if (PLANNER_ONLY.has(name)) return ["planner"];
  if (WORKER_ONLY.has(name)) return ["all-in-one", "worker"];
  return ["all-in-one", "worker", "planner"];
}

export const ROLE_TONE: Record<RoleId, { border: string; color: string }> = {
  "all-in-one": { border: "border-control", color: "text-muted" },
  worker: { border: "border-accent/40", color: "text-accent" },
  planner: { border: "border-[#b794f6]/40", color: "text-[#b794f6]" },
};

export interface LoopStep { tool: string; why: string }
export interface RoleLoop { role: RoleId; intro: string; steps: LoopStep[]; avoid: string }

export const ROLE_LOOPS: RoleLoop[] = [
  {
    role: "all-in-one",
    intro: "One agent that plans, builds and reviews — the default when a key has no seat. It still files work into review; it can't sign off what it built.",
    steps: [
      { tool: "register_agent", why: "Join without a seat" },
      { tool: "search_memory", why: "Check lessons for the area" },
      { tool: "claim_cluster", why: "Take work, reserve files" },
      { tool: "update_item", why: "Report and move to review" },
      { tool: "claim_review", why: "Pick up someone else's work" },
      { tool: "sign_off", why: "…or bounce with a reason" },
      { tool: "add_memory", why: "Leave what you learned" },
    ],
    avoid: "Avoid claim_next. It leaves the area-reservation table empty, so Live and collision checks see nothing held.",
  },
  {
    role: "worker",
    intro: "Builds what it claims. Paste the seat from Fleet → Wave into the prompt, not the MCP config.",
    steps: [
      { tool: "register_agent", why: "Join with the worker seat" },
      { tool: "heartbeat", why: "On the returned interval" },
      { tool: "claim_cluster", why: "Take an allocated cluster" },
      { tool: "update_item", why: "Touchpoints + evidence" },
      { tool: "claim_review", why: "Review a peer" },
      { tool: "add_memory", why: "Leave what you learned" },
    ],
    avoid: "",
  },
  {
    role: "planner",
    intro: "Partitions and allocates. Doesn't claim work itself.",
    steps: [
      { tool: "register_agent", why: "Join with the planner seat" },
      { tool: "collision_clusters", why: "Partition ready work" },
      { tool: "propose_allocation", why: "Offer clusters to workers" },
      { tool: "create_prd", why: "Draft PRDs from requests" },
      { tool: "heartbeat", why: "Stay visible on Live" },
    ],
    avoid: "claim_cluster and sign_off are in tools_off_limits for planners. Three refusals in a row and the server flags the agent.",
  },
];

interface Overlay {
  short: string;
  when: string;
  example: string;
  returns: string;
  refused: string;
  next: string[];
}

/**
 * Curated copy from Build.dc.html for the tools the drawing names. Everything else
 * derives from the live catalog — a missing overlay is not a missing tool.
 */
const OVERLAY: Record<string, Overlay> = {
  get_context: {
    short: "Project, role and the rules in force",
    when: "Call once at the start of a session to learn which project the key points at, the conventions in force and the top published lessons for this area.",
    example: 'get_context(project="GRPH")',
    returns: "Project summary, your role (if registered), active wave, top lessons.",
    refused: "",
    next: ["register_agent"],
  },
  register_agent: {
    short: "Join the roster; get your role",
    when: "Call before any write. With a seat you join the fleet in that role; without one you're all-in-one. Label yourself <model> @ <host> so Outposts can group you.",
    example: 'register_agent(\n  enrolment_code="seat_w7_4kq2",\n  label="claude-sonnet @ ana-mbp")',
    returns: "agent_id, heartbeat interval, and tools_off_limits for your role.",
    refused: "the seat was already taken or its wave has ended.",
    next: ["heartbeat", "claim_cluster"],
  },
  heartbeat: {
    short: "Stay on the roster; tell Live what you're doing",
    when: "Call on the interval register_agent returned. status and files are what the Live page shows — keep them honest.",
    example: 'heartbeat(\n  status="wiring nav section state",\n  files=["web/src/components/shell/LeftNav.tsx"])',
    returns: "ok, plus any directive (e.g. release a stale claim).",
    refused: "",
    next: [],
  },
  claim_cluster: {
    short: "Take non-colliding work and reserve its files",
    when: "The one way to take work. It claims every item in the cluster atomically and reserves the files they touch, so no other agent is handed work that collides with yours.",
    example: 'claim_cluster(cluster_id="C-118")',
    returns: "Items claimed, reserved areas, branch name.",
    refused: "another agent holds an overlapping area, or you're a planner.",
    next: ["update_item"],
  },
  update_item: {
    short: "Write status, touchpoints and evidence back",
    when: "Report progress on a claimed item. Moving to review needs evidence: name the check that produced it, not just 'tests pass'.",
    example: 'update_item(\n  id="GRPH-941", status="review",\n  evidence="test — LeftNav.test.tsx 14 passed")',
    returns: "The updated item.",
    refused: "you don't hold the item, or status=done (that's sign_off).",
    next: ["claim_review"],
  },
  claim_review: {
    short: "Take someone else's finished work",
    when: "Pull an item from the review pool. You're never handed your own work.",
    example: "claim_review()",
    returns: "The item, its diff summary and evidence.",
    refused: "the only items waiting are yours.",
    next: ["sign_off", "bounce"],
  },
  sign_off: {
    short: "Approve reviewed work",
    when: "Moves the item to done. Only after you've checked the evidence yourself.",
    example: 'sign_off(id="GRPH-940",\n  note="ran LeftNav tests; nav stays open")',
    returns: "The item, now done.",
    refused: "you built it — self-approval is refused unless the owner turned on self-review.",
    next: [],
  },
  bounce: {
    short: "Send work back with a failing check",
    when: "Return the item to its builder. A bounce must cite what failed; the planner reverts bounces with no evidence.",
    example: 'bounce(id="GRPH-905",\n  reason="decider.py still reads flags at boot")',
    returns: "The item, back in progress.",
    refused: "no reason is given.",
    next: [],
  },
  collision_clusters: {
    short: "Partition ready work by shared paths",
    when: "Planner only. Groups ready items so no two clusters touch the same files.",
    example: "collision_clusters()",
    returns: "Clusters with items, areas and any holds.",
    refused: "you're not a planner. Three refusals in a row and the server flags you.",
    next: ["propose_allocation"],
  },
  propose_allocation: {
    short: "Hand a cluster to a worker",
    when: "Planner only. Offers a cluster to a specific worker; they still claim it themselves.",
    example: 'propose_allocation(cluster_id="C-118", agent_id="A-12")',
    returns: "Allocation record.",
    refused: "the worker already holds a cluster.",
    next: [],
  },
  create_prd: {
    short: "Draft a PRD for grilling",
    when: "Start a PRD from a request. It stays in grilling until every question is answered.",
    example: 'create_prd(title="Hosted nav sections", request="R-311")',
    returns: "PRD id and its first grill questions.",
    refused: "",
    next: [],
  },
  search_memory: {
    short: "Find published lessons before you act",
    when: "Search before touching an unfamiliar area. Only published lessons come back — candidates never surface.",
    example: 'search_memory(query="hosted left nav section state")',
    returns: "Lessons with score and source item.",
    refused: "",
    next: [],
  },
  add_memory: {
    short: "Record a lesson for the next agent",
    when: "Write what you learned the hard way. It enters Memory as a candidate and is only searchable after review.",
    example: 'add_memory(\n  text="Nav open state comes from the route, not local state.",\n  kind="pitfall", source="GRPH-941")',
    returns: "Candidate id.",
    refused: "",
    next: [],
  },
  describe_code: {
    short: "Upsert modules, files, symbols and edges",
    when: "Describe what you read as you work so the Code graph fills in. Upserts — safe to repeat.",
    example: 'describe_code(nodes=[\n  {"path":"web/src/lib/routes.ts","kind":"file"}])',
    returns: "Counts of nodes and edges written.",
    refused: "",
    next: [],
  },
};

function firstSentence(text: string): string {
  const cut = text.split(/(?<=\.)\s/)[0] ?? text;
  return cut.trim();
}

function generatedExample(name: string, params: McpParamDetail[]): string {
  const req = params.filter((p) => p.required).map((p) => `${p.name}=…`);
  return req.length ? `${name}(${req.join(", ")})` : `${name}()`;
}

export interface EnrichedTool {
  info: McpToolInfo;
  tier: ToolTier;
  group: string;
  short: string;
  when: string;
  example: string;
  returns: string;
  refused: string;
  next: string[];
  roles: RoleId[];
}

export function enrich(info: McpToolInfo): EnrichedTool {
  const over = OVERLAY[info.name];
  const details = info.param_details ?? [];
  return {
    info,
    tier: tierOf(info.name),
    group: groupIdOf(info.name),
    short: over?.short ?? firstSentence(info.description),
    when: over?.when ?? info.description,
    example: over?.example ?? generatedExample(info.name, details),
    returns: over?.returns ?? "",
    refused: over?.refused ?? "",
    next: over?.next ?? [],
    roles: rolesOf(info.name),
  };
}

export function fmtCalls(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}
