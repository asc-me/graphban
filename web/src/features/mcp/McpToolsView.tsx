import * as React from "react";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { PlannerError } from "@/components/planner/PlannerStates";
import { cn } from "@/lib/cn";
import { useMcpTools } from "@/lib/queries";
import { settingsPath } from "@/lib/routes";
import type { McpParamDetail, McpToolInfo } from "@/lib/types";
import { Link } from "react-router-dom";

// ── Tool grouping ─────────────────────────────────────────────────────────────
// Groups mirror the agent's decision sequence: orient → claim → work → hand off.
// A tool in the wrong group would make the loop-by-role section incoherent, so
// the groups are the same vocabulary both sections share.

const GROUPS: { label: string; hint: string; prefix: string[] }[] = [
  { label: "Start here", hint: "Orient before anything else", prefix: ["get_context", "list_projects", "setup_project"] },
  { label: "Claim work", hint: "Take an item — never pick one by hand", prefix: ["claim_next", "claim_cluster", "claim_review", "next_cluster"] },
  { label: "Work items", hint: "Read, write, advance", prefix: ["get_item_details", "update_item", "create_item", "search_items", "get_backlog", "suggest_next", "release_item"] },
  { label: "Code graph", hint: "Describe and query the structure", prefix: ["get_code_map", "describe_code", "search_code", "code_neighbors", "graph_query", "link_code", "unlink_code"] },
  { label: "PRDs", hint: "Author, decompose, accept", prefix: ["create_prd", "get_prd", "update_prd", "decompose_prd", "grill_prd", "answer_grill", "prd_coverage", "prd_acceptance", "close_prd", "submit_verdict", "request_rebaseline"] },
  { label: "Fleet", hint: "Multi-agent coordination", prefix: ["register_agent", "heartbeat", "fleet_status", "delegate", "mint_enrolment", "assign_role", "retire_wave", "propose_allocation", "collision_clusters"] },
  { label: "Memory & lessons", hint: "Record and recall knowledge", prefix: ["add_memory", "search_memory", "get_lessons", "extract_lessons", "publish_memory", "reject_memory", "learning_loop", "review_recommendation"] },
  { label: "Relationships", hint: "Link items, trace neighbourhoods", prefix: ["link_items", "unlink_items", "related_work"] },
  { label: "Governance", hint: "Sign off or send back", prefix: ["sign_off", "bounce"] },
  { label: "Meta", hint: "Digests, bug reports", prefix: ["generate_digest", "report_graphban_issue"] },
];

function groupFor(name: string): string {
  for (const g of GROUPS) {
    if (g.prefix.includes(name)) return g.label;
  }
  return "Meta";
}

// ── Role loops ────────────────────────────────────────────────────────────────
// The ordered call sequence per role, the why per step, and what to avoid.
// Derived from the tool descriptions and the AGENTS.md operating loop.

interface LoopStep { tool: string; why: string; avoid: string }

const ROLE_LOOPS: { role: string; color: string; steps: LoopStep[] }[] = [
  {
    role: "worker",
    color: "#c6f24e",
    steps: [
      { tool: "register_agent", why: "Announce yourself before claiming anything — the ledger needs to know who you are.", avoid: "Calling claim_next first: without registration you have no agent_id and claims are refused." },
      { tool: "heartbeat", why: "Extend your lease and say what you are doing. Without it, your item looks abandoned and is reclaimed.", avoid: "Silent heartbeats — pass status and files so the Live page can show your work." },
      { tool: "claim_next", why: "Take one ready item. Two agents never get the same one, but it reserves NO files — prefer claim_cluster in a fleet.", avoid: "Picking an item by hand from the backlog. claim_next enforces the partition." },
      { tool: "get_item_details", why: "Read the full record before writing code — the description usually names the trap.", avoid: "Starting from the title alone. The bounce_reason field has caught three attempts that skipped this." },
      { tool: "update_item", why: "Record evidence as you get it. A status change with no receipt is indistinguishable from a placeholder move.", avoid: "Saving one summary at the end. APPENDS is the design — add receipts as you go." },
    ],
  },
  {
    role: "planner",
    color: "#7ca2ff",
    steps: [
      { tool: "get_context", why: "Orient: the project, your scopes, the gitops bar. Call this first.", avoid: "Assuming you know the project. The gitops fields measure things; unset means unmeasured, not absent." },
      { tool: "create_prd", why: "Author the spec. Use ## headings — decompose_prd turns each into tracked work.", avoid: "Writing without sections. A PRD without headings produces no tasks." },
      { tool: "grill_prd", why: "Surface unstated assumptions before approval. Approval is earned, not picked.", avoid: "Skipping the grill to ship faster. An ungrilled PRD ships assumptions as requirements." },
      { tool: "decompose_prd", why: "One task per uncovered section. create=true files them when approved.", avoid: "Decomposing a draft. Wait for approval or the tasks encode decisions that haven't been made." },
      { tool: "propose_allocation", why: "What the fleet should look like given who is online and what is ready.", avoid: "Assigning directly. A proposal lets you see the shape before committing." },
    ],
  },
  {
    role: "reviewer",
    color: "#a78bfa",
    steps: [
      { tool: "register_agent", why: "Same as worker — the ledger needs your identity.", avoid: "Reviewing without registering: your sign_off would have no agent_id to record." },
      { tool: "claim_review", why: "Lease an item in review that you did NOT build. Refused for your own work.", avoid: "Picking your own item. The server refuses it; the rule is the point." },
      { tool: "get_item_details", why: "Read what was built and what the bounce_reason says, if any.", avoid: "Reviewing from the title. The evidence field carries the receipts." },
      { tool: "sign_off", why: "Take a reviewed item to done. Refused if you built it.", avoid: "Signing off without reading the evidence. A sign_off with no sabotage receipt above the effort threshold is refused." },
      { tool: "bounce", why: "Send it back with a reason. Reserved for its author for one lease period.", avoid: "Bouncing without a reason. The reason is the load-bearing part — it is what the author reads." },
    ],
  },
];

// ── Connect-an-agent snippets ─────────────────────────────────────────────────

const CLIENT_SNIPPETS: { label: string; lang: string; code: string }[] = [
  {
    label: "Qwen Code",
    lang: "toml",
    code: `# .qwen/settings.json — add the MCP server
{
  "mcpServers": {
    "graphban": {
      "url": "http://localhost:8000/api/mcp",
      "headers": { "X-API-Key": "gb-..." }
    }
  }
}`,
  },
  {
    label: "Claude Code",
    lang: "toml",
    code: `# .claude/settings.json
{
  "mcpServers": {
    "graphban": {
      "url": "http://localhost:8000/api/mcp",
      "headers": { "X-API-Key": "gb-..." }
    }
  }
}`,
  },
  {
    label: "Cursor",
    lang: "json",
    code: `// .cursor/mcp.json
{
  "mcpServers": {
    "graphban": {
      "url": "http://localhost:8000/api/mcp",
      "headers": { "X-API-Key": "gb-..." }
    }
  }
}`,
  },
  {
    label: "Codex",
    lang: "toml",
    code: `# .codex/mcp.json
{
  "mcpServers": {
    "graphban": {
      "type": "url",
      "url": "http://localhost:8000/api/mcp",
      "headers": { "X-API-Key": "gb-..." }
    }
  }
}`,
  },
];

// ── Main view ─────────────────────────────────────────────────────────────────

export function McpToolsView() {
  const { data, isLoading, isError, refetch } = useMcpTools();

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
        <div className="mx-auto max-w-[1200px] space-y-8 p-5">
          <ConnectAgent />
          <LoopByRole tools={data.tools} />
          <ToolReference tools={data.tools} />
        </div>
      </div>
    </div>
  );
}

function fmt(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

// ── Connect-an-agent ──────────────────────────────────────────────────────────

function ConnectAgent() {
  const [activeClient, setActiveClient] = React.useState(0);
  const [copied, setCopied] = React.useState(false);
  const snippet = CLIENT_SNIPPETS[activeClient];

  const copy = React.useCallback(() => {
    void navigator.clipboard.writeText(snippet.code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [snippet.code]);

  return (
    <section className="rounded-[13px] border border-line-2 bg-surface-2">
      <div className="border-b border-line px-4 py-3">
        <h2 className="text-[14px] font-semibold text-fg">Connect an agent</h2>
        <p className="mt-0.5 text-[12px] leading-relaxed text-muted">
          Add the MCP server to your agent&apos;s config. The API key lives in{" "}
          <Link to={settingsPath("project/api-keys")} className="text-accent hover:underline">
            Settings → API keys
          </Link>.
        </p>
      </div>
      <div className="p-4">
        <div className="mb-3 flex flex-wrap gap-1.5">
          {CLIENT_SNIPPETS.map((c, i) => (
            <button
              key={c.label}
              onClick={() => setActiveClient(i)}
              className={cn(
                "rounded-lg border px-2.5 py-1 text-[11.5px] transition-colors",
                i === activeClient
                  ? "border-accent/50 bg-accent/10 text-accent"
                  : "border-line-2 bg-surface text-muted hover:border-line-hover hover:text-fg",
              )}
            >
              {c.label}
            </button>
          ))}
        </div>
        <div className="relative rounded-lg border border-line bg-[#0d1117]">
          <pre className="overflow-x-auto p-3.5 font-mono text-[11.5px] leading-relaxed text-fg-2">
            {snippet.code}
          </pre>
          <button
            onClick={copy}
            className="absolute right-2 top-2 rounded border border-line-2 bg-surface-2 px-2 py-0.5 font-mono text-[10px] text-muted transition-colors hover:border-line-hover hover:text-fg"
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      </div>
    </section>
  );
}

// ── Loop by role ──────────────────────────────────────────────────────────────

function LoopByRole({ tools }: { tools: McpToolInfo[] }) {
  const [activeRole, setActiveRole] = React.useState(0);
  const toolMap = React.useMemo(() => {
    const m: Record<string, McpToolInfo> = {};
    tools.forEach((t) => (m[t.name] = t));
    return m;
  }, [tools]);
  const loop = ROLE_LOOPS[activeRole];

  return (
    <section className="rounded-[13px] border border-line-2 bg-surface-2">
      <div className="border-b border-line px-4 py-3">
        <h2 className="text-[14px] font-semibold text-fg">The loop, by role</h2>
        <p className="mt-0.5 text-[12px] leading-relaxed text-muted">
          The ordered call sequence — why each step, and what goes wrong if you skip it.
        </p>
      </div>
      <div className="p-4">
        <div className="mb-4 flex gap-1.5">
          {ROLE_LOOPS.map((r, i) => (
            <button
              key={r.role}
              onClick={() => setActiveRole(i)}
              className={cn(
                "rounded-lg border px-2.5 py-1 text-[11.5px] transition-colors",
                i === activeRole
                  ? "border-line-hover bg-surface-3 text-fg"
                  : "border-line-2 bg-surface text-muted hover:border-line-hover hover:text-fg",
              )}
            >
              <span className="mr-1.5 inline-block h-2 w-2 rounded-full" style={{ background: r.color }} />
              {r.role}
            </button>
          ))}
        </div>
        <ol className="space-y-2">
          {loop.steps.map((step, i) => {
            const tool = toolMap[step.tool];
            return (
              <li key={step.tool} className="rounded-lg border border-line bg-surface p-3">
                <div className="mb-1 flex items-center gap-2">
                  <span className="flex h-5 w-5 flex-none items-center justify-center rounded-full border border-line-2 font-mono text-[10px] text-faint">
                    {i + 1}
                  </span>
                  <span className="font-mono text-[12px]" style={{ color: loop.color }}>
                    {step.tool}
                  </span>
                  {tool && (
                    <span className="ml-auto font-mono text-[10px] text-faint">{fmt(tool.calls)} calls</span>
                  )}
                </div>
                <p className="mb-1.5 text-[12px] leading-relaxed text-muted">{step.why}</p>
                <p className="text-[11px] leading-relaxed text-st-review/80">
                  <span className="font-mono text-[9px] uppercase tracking-wide text-st-review/60">Avoid: </span>
                  {step.avoid}
                </p>
              </li>
            );
          })}
        </ol>
      </div>
    </section>
  );
}

// ── Tool reference ────────────────────────────────────────────────────────────

function ToolReference({ tools }: { tools: McpToolInfo[] }) {
  const [query, setQuery] = React.useState("");
  const [selected, setSelected] = React.useState<McpToolInfo | null>(null);

  const grouped = React.useMemo(() => {
    const q = query.toLowerCase().trim();
    const filtered = q
      ? tools.filter(
          (t) =>
            t.name.toLowerCase().includes(q) ||
            t.description.toLowerCase().includes(q) ||
            t.params.some((p) => p.toLowerCase().includes(q)),
        )
      : tools;

    const map = new Map<string, McpToolInfo[]>();
    for (const g of GROUPS) map.set(g.label, []);
    map.set("Meta", []);
    for (const t of filtered) {
      const key = groupFor(t.name);
      map.get(key)?.push(t);
    }
    return [...map.entries()].filter(([, v]) => v.length > 0);
  }, [tools, query]);

  const hasResults = grouped.length > 0;

  return (
    <section className="rounded-[13px] border border-line-2 bg-surface-2">
      <div className="border-b border-line px-4 py-3">
        <div className="flex items-center gap-3">
          <h2 className="text-[14px] font-semibold text-fg">Tool reference</h2>
          <span className="font-mono text-[10px] text-faint">{tools.length} tools</span>
        </div>
        <div className="relative mt-2.5">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search tools, params, descriptions…"
            aria-label="Search tools"
            className="w-full rounded-lg border border-line-2 bg-surface px-3 py-1.5 pr-8 text-[12px] text-fg placeholder:text-faint focus:border-line-hover focus:outline-none"
          />
          {query && (
            <button
              onClick={() => setQuery("")}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-faint hover:text-fg"
              aria-label="Clear search"
            >
              ×
            </button>
          )}
        </div>
      </div>

      <div className="flex min-h-[400px]">
        <div className="min-h-0 flex-1 overflow-y-auto border-r border-line p-3">
          {!hasResults ? (
            <div className="flex h-full flex-col items-center justify-center px-4 py-12 text-center">
              <p className="text-[13px] text-muted">No tool matches.</p>
              <p className="mt-1 text-[12px] text-faint">
                Try what you want to do, e.g. &quot;review&quot; or &quot;files&quot;.
              </p>
            </div>
          ) : (
            <div className="space-y-4">
              {grouped.map(([groupLabel, groupTools]) => {
                const meta = GROUPS.find((g) => g.label === groupLabel);
                return (
                  <div key={groupLabel}>
                    <div className="mb-1.5 flex items-baseline gap-2">
                      <h3 className="font-mono text-[11px] uppercase tracking-wide text-faint">
                        {groupLabel}
                      </h3>
                      {meta && (
                        <span className="text-[10.5px] text-faint-2">{meta.hint}</span>
                      )}
                    </div>
                    <div className="space-y-0.5">
                      {groupTools.map((t) => (
                        <ToolRow
                          key={t.name}
                          tool={t}
                          active={selected?.name === t.name}
                          onClick={() => setSelected(t)}
                        />
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {selected && (
          <div className="w-[380px] flex-none overflow-y-auto">
            <ToolDetail tool={selected} onClose={() => setSelected(null)} />
          </div>
        )}
      </div>
    </section>
  );
}

function ToolRow({ tool, active, onClick }: { tool: McpToolInfo; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left transition-colors",
        active ? "bg-surface-4" : "hover:bg-surface-3",
      )}
    >
      <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-accent">{tool.name}</span>
      <span className="flex-none font-mono text-[10px] text-faint">{fmt(tool.calls)}</span>
      <span className="flex-none rounded border border-[#1c2620] bg-[rgba(95,208,122,0.06)] px-1 py-px font-mono text-[8px] uppercase tracking-wide text-st-done">
        live
      </span>
    </button>
  );
}

// ── Tool detail panel ─────────────────────────────────────────────────────────

function ToolDetail({ tool, onClose }: { tool: McpToolInfo; onClose: () => void }) {
  const group = groupFor(tool.name);
  return (
    <div className="p-4">
      <div className="mb-2 flex items-center gap-2">
        <span className="font-mono text-[13px] text-accent">{tool.name}</span>
        <button onClick={onClose} className="ml-auto text-faint hover:text-fg" aria-label="Close detail">
          ×
        </button>
      </div>
      <div className="mb-3 flex items-center gap-2">
        <span className="rounded border border-line-2 bg-surface px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide text-faint">
          {group}
        </span>
        <span className="font-mono text-[10px] text-faint">{fmt(tool.calls)} calls</span>
      </div>
      <p className="mb-4 text-[12px] leading-relaxed text-muted">{tool.description}</p>

      {tool.param_details.length > 0 ? (
        <div>
          <h4 className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">Parameters</h4>
          <div className="space-y-2">
            {tool.param_details.map((p) => (
              <ParamRow key={p.name} param={p} />
            ))}
          </div>
        </div>
      ) : (
        <div className="rounded-lg border border-line bg-surface p-2.5">
          <span className="font-mono text-[11px] text-faint">No parameters — this tool takes no input.</span>
        </div>
      )}
    </div>
  );
}

function ParamRow({ param }: { param: McpParamDetail }) {
  return (
    <div className="rounded-lg border border-line bg-surface p-2.5">
      <div className="mb-0.5 flex items-center gap-2">
        <span className="font-mono text-[11.5px] text-fg-2">{param.name}</span>
        <span className="font-mono text-[10px] text-faint">{param.type}</span>
        {param.required && (
          <span className="rounded border border-st-blocked/40 bg-st-blocked/10 px-1 py-px font-mono text-[8px] uppercase tracking-wide text-st-blocked">
            required
          </span>
        )}
      </div>
      {param.description && (
        <p className="text-[11px] leading-relaxed text-muted">{param.description}</p>
      )}
      {param.enum && param.enum.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-1">
          {param.enum.map((v) => (
            <span key={v} className="rounded border border-line-2 bg-surface-2 px-1 py-px font-mono text-[9px] text-muted-2">
              {v}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
