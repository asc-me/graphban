import * as React from "react";
import { Link } from "react-router-dom";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { PlannerError } from "@/components/planner/PlannerStates";
import { useMcpTools } from "@/lib/queries";
import { settingsPath } from "@/lib/routes";
import { cn } from "@/lib/cn";
import type { McpToolInfo, McpParamDetail } from "@/lib/types";

// ── Tool grouping ───────────────────────────────────────────────────────────
const GROUPS: { label: string; prefix: string[] }[] = [
  { label: "Orient", prefix: ["get_context", "list_projects", "create_project", "setup_project"] },
  { label: "Items", prefix: ["create_item", "update_item", "search_items", "get_item_details", "suggest_next", "related_work", "link_items", "unlink_items"] },
  { label: "Backlog & claims", prefix: ["get_backlog", "claim_next", "claim_cluster", "claim_review", "next_cluster", "release_item"] },
  { label: "PRDs", prefix: ["create_prd", "get_prd", "update_prd", "decompose_prd", "grill_prd", "answer_grill", "prd_coverage", "prd_acceptance", "request_rebaseline", "submit_verdict", "close_prd"] },
  { label: "Fleet", prefix: ["register_agent", "heartbeat", "fleet_status", "propose_allocation", "assign_role", "delegate", "mint_enrolment", "retire_wave", "collision_clusters"] },
  { label: "Code graph", prefix: ["describe_code", "get_code_map", "code_neighbors", "graph_query", "search_code", "link_code", "unlink_code"] },
  { label: "Memory", prefix: ["add_memory", "search_memory", "get_lessons", "publish_memory", "reject_memory", "extract_lessons"] },
  { label: "Learning", prefix: ["learning_loop", "review_recommendation"] },
];

function groupFor(name: string): string {
  for (const g of GROUPS) {
    if (g.prefix.includes(name)) return g.label;
  }
  return "Other";
}

function groupOrder(label: string): number {
  const idx = GROUPS.findIndex((g) => g.label === label);
  return idx === -1 ? GROUPS.length : idx;
}

// ── Connect-an-agent snippets ───────────────────────────────────────────────
const CLIENTS: { name: string; lang: string; snippet: string }[] = [
  {
    name: "Qwen Code",
    lang: "json",
    snippet: `{
  "mcpServers": {
    "graphban": {
      "url": "http://localhost:8000/api/mcp",
      "headers": { "X-API-Key": "<your-key>" }
    }
  }
}`,
  },
  {
    name: "Claude Code",
    lang: "json",
    snippet: `{
  "mcpServers": {
    "graphban": {
      "type": "url",
      "url": "http://localhost:8000/api/mcp",
      "headers": { "X-API-Key": "<your-key>" }
    }
  }
}`,
  },
  {
    name: "Cursor",
    lang: "json",
    snippet: `{
  "mcpServers": {
    "graphban": {
      "url": "http://localhost:8000/api/mcp",
      "headers": { "X-API-Key": "<your-key>" }
    }
  }
}`,
  },
  {
    name: "Codex",
    lang: "json",
    snippet: `{
  "mcpServers": {
    "graphban": {
      "type": "url",
      "url": "http://localhost:8000/api/mcp",
      "headers": { "X-API-Key": "<your-key>" }
    }
  }
}`,
  },
];

// ── Loop by role ────────────────────────────────────────────────────────────
interface RoleStep {
  tool: string;
  why: string;
  avoid: string;
}

const ROLE_LOOPS: { role: string; color: string; steps: RoleStep[] }[] = [
  {
    role: "worker",
    color: "#5fd07a",
    steps: [
      { tool: "register_agent", why: "Announce yourself so the ledger knows you exist", avoid: "Skipping this — heartbeats have no agent to extend" },
      { tool: "claim_next", why: "Take one ready item; two agents never get the same one", avoid: "claim_cluster when you are alone — it reserves files for nothing" },
      { tool: "heartbeat", why: "Extend your lease and say what you are doing", avoid: "Silence — the item looks abandoned and gets reclaimed" },
      { tool: "update_item", why: "Record evidence as you get it (APPENDS, never replaces)", avoid: "A status move with no receipt — indistinguishable from a placeholder" },
      { tool: "release_item", why: "Return what you cannot finish, so someone else can", avoid: "Leaving a claim nobody is working" },
    ],
  },
  {
    role: "planner",
    color: "#c9b8ff",
    steps: [
      { tool: "create_prd", why: "Author the durable handoff artifact with ## sections", avoid: "Vague intents — the grill will find them anyway" },
      { tool: "grill_prd", why: "Surface unstated assumptions before approval", avoid: "Skipping — an ungrilled PRD ships with holes the build cannot find" },
      { tool: "decompose_prd", why: "One task per uncovered section, each carrying the framing", avoid: "Manual item creation — the decomposition knows the sections" },
      { tool: "propose_allocation", why: "Size the fleet for what is ready now", avoid: "Allocating for the whole backlog — ready items change daily" },
      { tool: "mint_enrolment", why: "Give each child agent a seat bounded by your credential", avoid: "Two seats for one agent — the second is rejected" },
    ],
  },
  {
    role: "reviewer",
    color: "#e0b34a",
    steps: [
      { tool: "register_agent", why: "Same as worker — the ledger must know who is reviewing", avoid: "Sharing a credential with the builder — separation is the point" },
      { tool: "claim_review", why: "Lease an item in review that you did NOT build", avoid: "Claiming your own work — sign_off refuses it anyway" },
      { tool: "bounce", why: "Send it back with a reason within one lease period", avoid: "Silent approval — a review without a verdict is not a review" },
      { tool: "sign_off", why: "Take it to done with evidence, including sabotage receipts", avoid: "Signing off without reading the diff — the receipt says what you reviewed" },
    ],
  },
];

// ── Main component ──────────────────────────────────────────────────────────
export function McpToolsView() {
  const { data, isLoading, isError, refetch } = useMcpTools();
  const [query, setQuery] = React.useState("");
  const [selectedTool, setSelectedTool] = React.useState<string | null>(null);
  const [copiedClient, setCopiedClient] = React.useState<string | null>(null);

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
  if (data.tools.length === 0) {
    return (
      <div className="p-6 text-[13px] text-muted">
        No tools are registered. That is a looked-at empty catalog, not a failed fetch.
      </div>
    );
  }

  const totalCalls = data.tools.reduce((s, t) => s + t.calls, 0);
  const q = query.trim().toLowerCase();

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
    for (const t of filtered) {
      const g = groupFor(t.name);
      if (!map.has(g)) map.set(g, []);
      map.get(g)!.push(t);
    }
    return [...map.entries()].sort(
      ([a], [b]) => groupOrder(a) - groupOrder(b),
    );
  }, [filtered]);

  const selected = selectedTool ? data.tools.find((t) => t.name === selectedTool) ?? null : null;

  const copySnippet = async (text: string, name: string) => {
    await navigator.clipboard.writeText(text);
    setCopiedClient(name);
    setTimeout(() => setCopiedClient(null), 2000);
  };

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
        <div className="mx-auto max-w-[1200px] space-y-6 p-5">
          {/* Connect an agent */}
          <section>
            <h2 className="mb-3 text-[13px] font-semibold uppercase tracking-wide text-fg-2">
              Connect an agent
            </h2>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {CLIENTS.map((c) => (
                <div
                  key={c.name}
                  className="rounded-[12px] border border-line-2 bg-surface-2 p-3"
                >
                  <div className="mb-2 flex items-center justify-between">
                    <span className="font-mono text-[11.5px] text-fg-2">{c.name}</span>
                    <button
                      onClick={() => copySnippet(c.snippet, c.name)}
                      className="rounded border border-line-2 px-1.5 py-0.5 font-mono text-[9.5px] text-muted transition-colors hover:border-line-hover hover:text-fg"
                    >
                      {copiedClient === c.name ? "copied" : "copy"}
                    </button>
                  </div>
                  <pre className="overflow-x-auto rounded-lg bg-surface p-2.5 font-mono text-[10px] leading-relaxed text-muted-2">
                    {c.snippet}
                  </pre>
                </div>
              ))}
            </div>
          </section>

          {/* The loop, by role */}
          <section>
            <h2 className="mb-3 text-[13px] font-semibold uppercase tracking-wide text-fg-2">
              The loop, by role
            </h2>
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
              {ROLE_LOOPS.map((r) => (
                <div
                  key={r.role}
                  className="rounded-[12px] border border-line-2 bg-surface-2 p-3.5"
                >
                  <div className="mb-3 flex items-center gap-2">
                    <span
                      className="h-2 w-2 rounded-full"
                      style={{ background: r.color }}
                    />
                    <span className="font-mono text-[11.5px] uppercase tracking-wide" style={{ color: r.color }}>
                      {r.role}
                    </span>
                  </div>
                  <ol className="space-y-2.5">
                    {r.steps.map((s, i) => (
                      <li key={s.tool} className="relative pl-6">
                        <span className="absolute left-0 top-0.5 flex h-4 w-4 items-center justify-center rounded-full border border-line-2 font-mono text-[9px] text-faint">
                          {i + 1}
                        </span>
                        <div className="font-mono text-[11px] text-accent">{s.tool}</div>
                        <div className="text-[11.5px] leading-relaxed text-muted">{s.why}</div>
                        <div className="mt-0.5 text-[10.5px] italic text-faint">
                          avoid: {s.avoid}
                        </div>
                      </li>
                    ))}
                  </ol>
                </div>
              ))}
            </div>
          </section>

          {/* Searchable reference */}
          <section>
            <div className="mb-3 flex items-center gap-3">
              <h2 className="text-[13px] font-semibold uppercase tracking-wide text-fg-2">
                Tool reference
              </h2>
              <div className="relative flex-1 max-w-[320px]">
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => e.key === "Escape" && setQuery("")}
                  placeholder="Search tools, params…"
                  aria-label="Search tools"
                  className="w-full rounded-lg border border-line-2 bg-surface-2 px-2.5 py-1 text-[11.5px] text-fg placeholder:text-faint focus:border-line-hover focus:outline-none"
                />
                {q && (
                  <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 font-mono text-[10px] text-faint">
                    {filtered.length}
                  </span>
                )}
              </div>
            </div>

            {filtered.length === 0 ? (
              <div className="rounded-[12px] border border-line-2 bg-surface-2 p-6 text-center">
                <p className="text-[12.5px] text-muted">
                  No tools match "<span className="font-mono text-fg-2">{query}</span>".
                </p>
                <p className="mt-1 text-[11.5px] text-faint">
                  Try the tool name, a parameter, or a word from the description.
                </p>
              </div>
            ) : (
              <div className="flex gap-4">
                {/* Grouped list */}
                <div className="min-w-0 flex-1 space-y-4">
                  {grouped.map(([group, tools]) => (
                    <div key={group}>
                      <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">
                        {group}
                      </div>
                      <div className="space-y-1">
                        {tools.map((t) => (
                          <button
                            key={t.name}
                            onClick={() => setSelectedTool(t.name)}
                            className={cn(
                              "flex w-full items-center gap-2 rounded-lg border px-3 py-2 text-left transition-colors",
                              selectedTool === t.name
                                ? "border-accent/50 bg-surface-3"
                                : "border-line-2 bg-surface-2 hover:border-line-hover",
                            )}
                          >
                            <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-accent">
                              {t.name}
                            </span>
                            <span className="flex-none font-mono text-[9.5px] text-faint">
                              {t.params.length} params
                            </span>
                            <span className="flex-none font-mono text-[9.5px] text-faint">
                              {fmt(t.calls)} calls
                            </span>
                          </button>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>

                {/* Detail panel */}
                {selected && (
                  <div className="w-[360px] flex-none rounded-[12px] border border-line-hover bg-surface-3/95 p-4">
                    <div className="mb-1.5 flex items-center gap-2">
                      <span className="font-mono text-[12.5px] text-accent">{selected.name}</span>
                      <span className="rounded border border-[#1c2620] bg-[rgba(95,208,122,0.06)] px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide text-st-done">
                        live
                      </span>
                      <button
                        onClick={() => setSelectedTool(null)}
                        className="ml-auto text-faint hover:text-fg"
                      >
                        ×
                      </button>
                    </div>
                    <p className="mb-3 text-[12px] leading-relaxed text-muted">
                      {selected.description}
                    </p>
                    <div className="mb-3 flex items-center gap-3 font-mono text-[10px] text-faint">
                      <span>{fmt(selected.calls)} calls</span>
                      <span>{selected.params.length} params</span>
                    </div>

                    {(selected.param_details?.length ?? 0) > 0 ? (
                      <div>
                        <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">
                          Parameters
                        </div>
                        <div className="space-y-2">
                          {selected.param_details!.map((p) => (
                            <ParamRow key={p.name} param={p} />
                          ))}
                        </div>
                      </div>
                    ) : selected.params.length > 0 ? (
                      <div>
                        <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">
                          Parameters
                        </div>
                        <div className="flex flex-wrap gap-1">
                          {selected.params.map((p) => (
                            <span key={p} className="rounded-md border border-line-2 bg-surface px-1.5 py-0.5 font-mono text-[10px] text-muted-2">
                              {p}
                            </span>
                          ))}
                        </div>
                      </div>
                    ) : (
                      <div className="font-mono text-[10px] text-faint">No parameters</div>
                    )}
                  </div>
                )}
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

function ParamRow({ param }: { param: McpParamDetail }) {
  return (
    <div className="rounded-lg border border-line-2 bg-surface/60 p-2">
      <div className="flex items-center gap-2">
        <span className="font-mono text-[11px] text-fg-2">{param.name}</span>
        <span className="font-mono text-[9.5px] text-faint">{param.type}</span>
        {param.required && (
          <span className="rounded border border-st-blocked/40 px-1 py-px font-mono text-[8.5px] uppercase text-st-blocked">
            required
          </span>
        )}
      </div>
      {param.description && (
        <p className="mt-1 text-[11px] leading-relaxed text-muted">{param.description}</p>
      )}
      {param.enum && param.enum.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-1">
          {param.enum.map((v) => (
            <span key={v} className="rounded border border-line-2 bg-surface-2 px-1 py-px font-mono text-[9.5px] text-muted-2">
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
