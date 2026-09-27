import * as React from "react";
import { Link } from "react-router-dom";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { PlannerError } from "@/components/planner/PlannerStates";
import { cn } from "@/lib/cn";
import { useMcpTools } from "@/lib/queries";
import { settingsPath } from "@/lib/routes";
import type { McpParamDetail, McpToolInfo } from "@/lib/types";

// ── Tool grouping ────────────────────────────────────────────────────────────
// The manifest has no group field, so the UI derives one from the name prefix.
// A tool that matches no known prefix lands in "Other" rather than being silently
// dropped — an ungrouped tool is visible; a missing one is not.

const GROUPS: { label: string; prefix: string[] }[] = [
  { label: "Orientation", prefix: ["get_context", "list_projects", "create_project", "setup_project"] },
  { label: "Items", prefix: ["create_item", "update_item", "search_items", "get_item_details", "suggest_next", "get_backlog", "link_items", "unlink_items", "related_work", "release_item"] },
  { label: "PRDs", prefix: ["prd_coverage", "decompose_prd", "create_prd", "get_prd", "update_prd", "answer_grill", "grill_prd", "prd_acceptance", "request_rebaseline", "submit_verdict", "close_prd"] },
  { label: "Memory", prefix: ["add_memory", "search_memory", "publish_memory", "reject_memory", "get_lessons", "extract_lessons"] },
  { label: "Fleet", prefix: ["next_cluster", "claim_next", "claim_cluster", "claim_review", "propose_allocation", "assign_role", "delegate", "collision_clusters", "sign_off", "bounce", "register_agent", "mint_enrolment", "retire_wave", "fleet_status", "heartbeat"] },
  { label: "Code graph", prefix: ["describe_code", "get_code_map", "code_neighbors", "graph_query", "search_code", "link_code", "unlink_code"] },
  { label: "Learning", prefix: ["learning_loop", "review_recommendation"] },
  { label: "Other", prefix: [] },
];

function groupFor(name: string): string {
  for (const g of GROUPS) {
    if (g.prefix.includes(name)) return g.label;
  }
  return "Other";
}

const GROUP_ORDER = GROUPS.map((g) => g.label);

// ── Role loops ───────────────────────────────────────────────────────────────
// "The loop, by role" — the ordered call sequence each role follows, why each
// step matters, and what to avoid. Static content derived from AGENTS.md.

interface LoopStep {
  tool: string;
  why: string;
  avoid: string;
}

const ROLE_LOOPS: { role: string; steps: LoopStep[] }[] = [
  {
    role: "Worker",
    steps: [
      { tool: "register_agent", why: "Announce yourself so the ledger knows who is building.", avoid: "Skipping this — heartbeat without registration has no identity." },
      { tool: "claim_next", why: "Take one ready item. Two agents never get the same one.", avoid: "claim_cluster for a single item — it reserves files you do not need." },
      { tool: "get_item_details", why: "Read the full record before writing code — the description names the trap.", avoid: "Coding from the title alone." },
      { tool: "heartbeat", why: "Extend the lease and say what you are doing. Without it, the item looks abandoned.", avoid: "Long silences between heartbeats." },
      { tool: "update_item", why: "Record what you did, with evidence. Status change with no receipt is a placeholder.", avoid: "Moving to review without evidence." },
    ],
  },
  {
    role: "Planner",
    steps: [
      { tool: "register_agent", why: "Same identity step — the ledger attributes decisions to you.", avoid: "Operating without registration." },
      { tool: "get_backlog", why: "See what is ready and what is blocked, with scores.", avoid: "Guessing priorities from item titles." },
      { tool: "collision_clusters", why: "Partition ready work into non-colliding sets before assigning.", avoid: "Hand-assigning without checking touchpoint overlap." },
      { tool: "propose_allocation", why: "Show what the fleet should look like before committing.", avoid: "Assigning without a proposal — the ledger records the plan." },
      { tool: "delegate", why: "Hand work to a child through the ledger, not just the prompt.", avoid: "Spawning without delegating — the child has no item to claim." },
    ],
  },
  {
    role: "Reviewer",
    steps: [
      { tool: "register_agent", why: "Identity — sign-offs are attributed.", avoid: "Reviewing without registration." },
      { tool: "claim_review", why: "Lease an item you did NOT build. Refused if only your own work is in review.", avoid: "Reviewing your own item — the ledger enforces separation." },
      { tool: "get_item_details", why: "Read the evidence and the brief before judging.", avoid: "Skimming — the description names what was tested." },
      { tool: "sign_off", why: "Take a reviewed item to done. Refused if you built it.", avoid: "Signing off without sabotage evidence above the effort threshold." },
      { tool: "bounce", why: "Send it back with a reason. Reserved for the author within one lease period.", avoid: "Bouncing without a specific reason." },
    ],
  },
];

// ── Connect-an-agent snippets ────────────────────────────────────────────────

const CLIENT_SNIPPETS: { client: string; lang: string; code: string }[] = [
  {
    client: "Qwen Code",
    lang: "text",
    code: `# In your prompt or .qwen/agent.md:
Register with register_agent using enrolment_code='WORKER-XXXX'
Then: claim_next, get_item_details, heartbeat, update_item`,
  },
  {
    client: "Claude Code",
    lang: "text",
    code: `# In CLAUDE.md or the prompt:
Register via mcp__graphban__register_agent
enrolment_code: "WORKER-XXXX"
Then: claim_next → get_item_details → heartbeat → update_item`,
  },
  {
    client: "Codex",
    lang: "text",
    code: `# In AGENTS.md:
Call register_agent with your enrolment_code.
Loop: claim_next, get_item_details, heartbeat, update_item.`,
  },
  {
    client: "Generic MCP",
    lang: "json",
    code: `{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "register_agent",
    "arguments": { "enrolment_code": "WORKER-XXXX" }
  }
}`,
  },
];

// ── Main view ────────────────────────────────────────────────────────────────

export function McpToolsView() {
  const { data, isLoading, isError, refetch } = useMcpTools();
  const [query, setQuery] = React.useState("");
  const [selectedTool, setSelectedTool] = React.useState<McpToolInfo | null>(null);
  const [activeGroup, setActiveGroup] = React.useState<string | null>(null);
  const [showLoop, setShowLoop] = React.useState(false);
  const [showConnect, setShowConnect] = React.useState(true);

  if (isLoading && !data) {
    return <div className="flex h-full items-center justify-center text-[13px] text-muted">Loading…</div>;
  }
  if (isError && !data) {
    return (
      <PlannerError
        message="MCP catalog unavailable — the tool list could not be fetched."
        onRetry={() => void refetch()}
      />
    );
  }
  if (!data) {
    return <div className="flex h-full items-center justify-center text-[13px] text-muted">Loading…</div>;
  }

  const totalCalls = data.tools.reduce((s, t) => s + t.calls, 0);

  // Group + filter
  const q = query.toLowerCase().trim();
  const filtered = q
    ? data.tools.filter(
        (t) =>
          t.name.toLowerCase().includes(q) ||
          t.description.toLowerCase().includes(q) ||
          t.params.some((p) => p.toLowerCase().includes(q)),
      )
    : data.tools;

  const grouped = React.useMemo(() => {
    const map = new Map<string, McpToolInfo[]>();
    for (const g of GROUP_ORDER) map.set(g, []);
    for (const t of filtered) {
      const g = groupFor(t.name);
      map.get(g)!.push(t);
    }
    // Remove empty groups
    for (const [k, v] of map) {
      if (v.length === 0) map.delete(k);
    }
    return map;
  }, [filtered]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PlaceHeader
        viewName="MCP Tools"
        purpose="The Model Context Protocol surface agents call. Same code path as the web app."
        action={
          <div className="flex items-center gap-3">
            <Link
              to={settingsPath("project/api-keys")}
              className="text-[12px] text-muted transition-colors hover:text-fg-2"
            >
              Looking for API keys?
            </Link>
            <div className="flex items-center gap-2 rounded-lg border border-[#1c2620] bg-[rgba(95,208,122,0.05)] px-2.5 py-1.5 font-mono text-[10.5px] text-st-done">
              <span className="blink h-1.5 w-1.5 rounded-full bg-st-done shadow-[0_0_8px_#5fd07a]" />
              {data.live} TOOLS LIVE · {fmt(totalCalls)} CALLS
            </div>
          </div>
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-[1200px] p-5">
          {/* Connect-an-agent */}
          {showConnect && (
            <ConnectBlock onClose={() => setShowConnect(false)} />
          )}

          {/* The loop, by role */}
          <div className="mb-5">
            <button
              onClick={() => setShowLoop((v) => !v)}
              className="flex w-full items-center justify-between rounded-xl border border-line-2 bg-surface-2 px-4 py-3 text-left transition-colors hover:border-line-hover"
            >
              <div>
                <h2 className="text-[13px] font-semibold text-fg-2">The loop, by role</h2>
                <p className="mt-0.5 text-[11.5px] text-muted">
                  The ordered call sequence per role, the why for each step, and what to avoid.
                </p>
              </div>
              <span className={cn("text-faint transition-transform", showLoop && "rotate-180")}>
                ▾
              </span>
            </button>
            {showLoop && (
              <div className="mt-2 grid gap-3 md:grid-cols-3">
                {ROLE_LOOPS.map((rl) => (
                  <div key={rl.role} className="rounded-xl border border-line-2 bg-surface-2 p-4">
                    <h3 className="mb-3 font-mono text-[11px] uppercase tracking-wide text-accent">
                      {rl.role}
                    </h3>
                    <ol className="space-y-2.5">
                      {rl.steps.map((s, i) => (
                        <li key={s.tool} className="relative pl-6">
                          <span className="absolute left-0 top-0.5 flex h-4 w-4 items-center justify-center rounded-full border border-line-3 bg-surface-3 font-mono text-[9px] text-faint">
                            {i + 1}
                          </span>
                          <div className="font-mono text-[11.5px] text-fg-2">{s.tool}</div>
                          <p className="mt-0.5 text-[11px] leading-relaxed text-muted">{s.why}</p>
                          <p className="mt-0.5 text-[10.5px] leading-relaxed text-faint">
                            <span className="font-medium text-st-review">Avoid:</span> {s.avoid}
                          </p>
                        </li>
                      ))}
                    </ol>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Search */}
          <div className="mb-4">
            <div className="relative">
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Escape" && setQuery("")}
                placeholder="Search tools… e.g. 'review', 'files', 'claim'"
                aria-label="Search MCP tools"
                className="w-full rounded-xl border border-line-2 bg-surface-2 px-4 py-2.5 text-[12.5px] text-fg placeholder:text-faint focus:border-line-hover focus:outline-none"
              />
              {query && (
                <button
                  onClick={() => setQuery("")}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-faint hover:text-fg"
                  aria-label="Clear search"
                >
                  ×
                </button>
              )}
            </div>
            {/* Group filter chips */}
            <div className="mt-2.5 flex flex-wrap gap-1.5">
              {GROUP_ORDER.filter((g) => g !== "Other").map((g) => (
                <button
                  key={g}
                  onClick={() => setActiveGroup(activeGroup === g ? null : g)}
                  className={cn(
                    "rounded-lg border px-2.5 py-1 text-[11px] transition-colors",
                    activeGroup === g
                      ? "border-line-hover bg-surface-3 text-fg"
                      : "border-line-2 bg-surface-2 text-muted hover:border-line-hover hover:text-fg-2",
                  )}
                >
                  {g}
                  <span className="ml-1.5 font-mono text-[10px] text-faint">
                    {grouped.get(g)?.length ?? 0}
                  </span>
                </button>
              ))}
            </div>
          </div>

          {/* Tool list */}
          {grouped.size === 0 ? (
            <div className="rounded-xl border border-line-2 bg-surface-2 px-6 py-10 text-center">
              <p className="text-[13px] text-muted">
                No tool matches{" "}
                <span className="font-mono text-fg-2">"{query}"</span>.
              </p>
              <p className="mt-1.5 text-[12px] text-faint">
                Try what you want to do, e.g. "review" or "files".
              </p>
            </div>
          ) : (
            <div className="flex gap-4">
              {/* Grouped list */}
              <div className="min-w-0 flex-1 space-y-5">
                {[...grouped.entries()]
                  .filter(([g]) => !activeGroup || g === activeGroup)
                  .map(([group, tools]) => (
                    <div key={group}>
                      <h3 className="mb-2 font-mono text-[10.5px] uppercase tracking-wide text-faint">
                        {group} · {tools.length}
                      </h3>
                      <div className="grid gap-2 md:grid-cols-2">
                        {tools.map((t) => (
                          <ToolRow
                            key={t.name}
                            tool={t}
                            selected={selectedTool?.name === t.name}
                            onSelect={() =>
                              setSelectedTool(selectedTool?.name === t.name ? null : t)
                            }
                          />
                        ))}
                      </div>
                    </div>
                  ))}
              </div>

              {/* Detail panel */}
              {selectedTool && (
                <div className="sticky top-4 w-[360px] flex-none self-start">
                  <ToolDetail tool={selectedTool} onClose={() => setSelectedTool(null)} />
                </div>
              )}
            </div>
          )}

          {data.tools.length === 0 && (
            <div className="rounded-xl border border-line-2 bg-surface-2 px-6 py-10 text-center">
              <p className="text-[13px] text-muted">
                No tools are registered. That is a looked-at empty catalog, not a failed fetch.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Connect block ────────────────────────────────────────────────────────────

function ConnectBlock({ onClose }: { onClose: () => void }) {
  const [activeClient, setActiveClient] = React.useState(0);
  const [copied, setCopied] = React.useState(false);
  const snippet = CLIENT_SNIPPETS[activeClient];

  const copy = React.useCallback(() => {
    navigator.clipboard.writeText(snippet.code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [snippet.code]);

  return (
    <div className="mb-5 rounded-xl border border-line-2 bg-surface-2 p-4">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h2 className="text-[13px] font-semibold text-fg-2">Connect an agent</h2>
          <p className="mt-0.5 text-[11.5px] text-muted">
            Paste an enrolment code into your agent's prompt. It registers, claims work, and builds.
          </p>
        </div>
        <button
          onClick={onClose}
          className="text-faint hover:text-fg"
          aria-label="Dismiss"
        >
          ×
        </button>
      </div>

      {/* Client tabs */}
      <div className="mb-3 flex gap-1">
        {CLIENT_SNIPPETS.map((cs, i) => (
          <button
            key={cs.client}
            onClick={() => setActiveClient(i)}
            className={cn(
              "rounded-lg border px-2.5 py-1 text-[11px] transition-colors",
              i === activeClient
                ? "border-line-hover bg-surface-3 text-fg"
                : "border-line-2 bg-surface text-muted hover:border-line-hover hover:text-fg-2",
            )}
          >
            {cs.client}
          </button>
        ))}
      </div>

      {/* Code block */}
      <div className="relative rounded-lg border border-line-2 bg-[#0d1114] p-3">
        <pre className="overflow-x-auto font-mono text-[11px] leading-relaxed text-muted">
          {snippet.code}
        </pre>
        <button
          onClick={copy}
          className={cn(
            "absolute right-2 top-2 rounded border px-2 py-0.5 font-mono text-[10px] transition-colors",
            copied
              ? "border-st-done/40 bg-st-done/10 text-st-done"
              : "border-line-3 bg-surface-2 text-faint hover:border-line-hover hover:text-fg",
          )}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}

// ── Tool row ─────────────────────────────────────────────────────────────────

function ToolRow({
  tool,
  selected,
  onSelect,
}: {
  tool: McpToolInfo;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      onClick={onSelect}
      className={cn(
        "w-full rounded-xl border p-3 text-left transition-colors",
        selected
          ? "border-accent/50 bg-surface-3"
          : "border-line-2 bg-surface-2 hover:border-line-hover",
      )}
    >
      <div className="mb-1 flex items-center gap-2">
        <span className="font-mono text-[12px] text-accent">{tool.name}</span>
        <span className="ml-auto font-mono text-[10px] text-faint">{fmt(tool.calls)} calls</span>
      </div>
      <p className="line-clamp-2 text-[11.5px] leading-relaxed text-muted">{tool.description}</p>
      {tool.params.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {tool.params.slice(0, 5).map((p) => (
            <span
              key={p}
              className="rounded border border-line-2 bg-surface px-1.5 py-0.5 font-mono text-[9.5px] text-muted-2"
            >
              {p}
            </span>
          ))}
          {tool.params.length > 5 && (
            <span className="px-1 py-0.5 font-mono text-[9.5px] text-faint">
              +{tool.params.length - 5}
            </span>
          )}
        </div>
      )}
    </button>
  );
}

// ── Tool detail panel ────────────────────────────────────────────────────────

function ToolDetail({ tool, onClose }: { tool: McpToolInfo; onClose: () => void }) {
  const group = groupFor(tool.name);
  const requiredParams = tool.param_details?.filter((p) => p.required) ?? [];
  const optionalParams = tool.param_details?.filter((p) => !p.required) ?? [];

  return (
    <div className="animate-fade rounded-[13px] border border-line-hover bg-surface-3/95 p-4 shadow-[0_20px_48px_rgba(0,0,0,0.5)]">
      <div className="mb-2 flex items-center gap-2">
        <span className="rounded border border-accent/40 bg-accent/5 px-1.5 py-px font-mono text-[9px] uppercase tracking-wide text-accent">
          live
        </span>
        <span className="font-mono text-[9px] uppercase tracking-wide text-faint">{group}</span>
        <button onClick={onClose} className="ml-auto text-faint hover:text-fg" aria-label="Close">
          ×
        </button>
      </div>

      <h3 className="mb-1.5 font-mono text-[14px] text-fg-2">{tool.name}</h3>
      <p className="mb-3 text-[12px] leading-relaxed text-muted">{tool.description}</p>

      {/* Stats */}
      <div className="mb-3 flex gap-3 border-t border-line pt-3">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-wide text-faint">Calls</div>
          <div className="font-mono text-[13px] text-fg-2">{fmt(tool.calls)}</div>
        </div>
        <div>
          <div className="font-mono text-[10px] uppercase tracking-wide text-faint">Params</div>
          <div className="font-mono text-[13px] text-fg-2">{tool.params.length}</div>
        </div>
      </div>

      {/* Parameters */}
      {tool.param_details && tool.param_details.length > 0 && (
        <div className="border-t border-line pt-3">
          <h4 className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">
            Parameters
          </h4>
          <div className="space-y-2">
            {requiredParams.map((p) => (
              <ParamRow key={p.name} param={p} />
            ))}
            {optionalParams.map((p) => (
              <ParamRow key={p.name} param={p} />
            ))}
          </div>
        </div>
      )}

      {/* Fallback: plain params list if no details */}
      {(!tool.param_details || tool.param_details.length === 0) && tool.params.length > 0 && (
        <div className="border-t border-line pt-3">
          <h4 className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">
            Parameters
          </h4>
          <div className="flex flex-wrap gap-1">
            {tool.params.map((p) => (
              <span
                key={p}
                className="rounded border border-line-2 bg-surface px-1.5 py-0.5 font-mono text-[10px] text-muted-2"
              >
                {p}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function ParamRow({ param }: { param: McpParamDetail }) {
  return (
    <div className="rounded-lg border border-line-2 bg-surface-2/60 p-2">
      <div className="flex items-center gap-1.5">
        <span className="font-mono text-[11px] text-fg-2">{param.name}</span>
        <span className="font-mono text-[9px] text-faint">{param.type}</span>
        {param.required && (
          <span className="rounded border border-st-review/40 px-1 py-px font-mono text-[8px] uppercase tracking-wide text-st-review">
            required
          </span>
        )}
      </div>
      {param.description && (
        <p className="mt-1 text-[10.5px] leading-relaxed text-muted">{param.description}</p>
      )}
      {param.enum && param.enum.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-0.5">
          {param.enum.map((v) => (
            <span
              key={v}
              className="rounded bg-surface px-1 py-px font-mono text-[9px] text-faint"
            >
              {v}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function fmt(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}
