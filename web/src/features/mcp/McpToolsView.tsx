import { Search } from "lucide-react";
import * as React from "react";
import { Link } from "react-router-dom";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import {
  PlannerEmpty,
  PlannerError,
  TableSkeleton,
} from "@/components/planner/PlannerStates";
import { useProjectCtx } from "@/features/ProjectContext";
import { CLIENTS } from "@/features/settings/McpInstall";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/cn";
import { useConfig, useMcpTools } from "@/lib/queries";
import { projectPath, settingsPath } from "@/lib/routes";
import type { McpParamDetail, McpToolInfo } from "@/lib/types";

import {
  GROUPS,
  ROLE_LOOPS,
  ROLE_TONE,
  TIER_TONE,
  enrich,
  fmtCalls,
  type EnrichedTool,
  type RoleId,
} from "./catalog";

/**
 * MCP Tools — connect, the loop by role, and the searchable reference (PRD-47 S5).
 *
 * Connect snippets come from `McpInstall` CLIENTS (verified against vendor docs), not
 * from a second JSON catalog at localhost:8000. Overlay fields the catalog endpoint
 * does not serve live in `catalog.ts` and are omitted when unknown.
 */
export function McpToolsView() {
  const { data, isLoading, isError, refetch } = useMcpTools();
  const [openName, setOpenName] = React.useState("claim_cluster");
  const select = React.useCallback((name: string) => setOpenName(name), []);

  if (isError && !data) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <Header />
        <PlannerError
          message="MCP catalog unavailable — the tool list could not be fetched."
          onRetry={() => void refetch()}
        />
      </div>
    );
  }
  if (isLoading && !data) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <Header />
        <div className="p-5">
          <TableSkeleton rows={8} columns={3} />
        </div>
      </div>
    );
  }
  if (!data || data.tools.length === 0) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <Header />
        <PlannerEmpty
          title="No tools are registered"
          description="That is a looked-at empty catalog, not a failed fetch."
        />
      </div>
    );
  }

  const totalCalls = data.tools.reduce((s, t) => s + t.calls, 0);

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="mcp-tools">
      <Header
        live={
          <div className="flex items-center gap-2 rounded-lg border border-[#1c2620] bg-[rgba(95,208,122,0.05)] px-2.5 py-1.5 font-mono text-meta text-st-done">
            <span className="blink h-1.5 w-1.5 rounded-full bg-st-done shadow-[0_0_8px_#5fd07a]" />
            {data.live} tools live · {fmtCalls(totalCalls)} calls
          </div>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex max-w-[1080px] flex-col gap-6 p-5">
          <ConnectAgent />
          <LoopByRole tools={data.tools} openName={openName} onSelect={select} />
          <ToolReference
            tools={data.tools}
            live={data.live}
            openName={openName}
            onSelect={select}
          />
        </div>
      </div>
    </div>
  );
}

function Header({ live }: { live?: React.ReactNode }) {
  const { active } = useProjectCtx();
  const { data: config } = useConfig();
  const fleetHref = config?.hosted_mode && active?.tag
    ? projectPath(active.tag, "fleet")
    : "/fleet";
  return (
    <PlaceHeader
      viewName="MCP Tools"
      purpose="How agents use the tools built into Graphban: connect, follow the loop for their role, and look up any call."
      action={
        <div className="flex items-center gap-3">
          <Link
            to={settingsPath("project/api-keys")}
            className="text-[12px] text-muted transition-colors hover:text-fg-2"
          >
            Looking for API keys?
          </Link>
          <Link
            to={fleetHref}
            className="text-[12px] text-muted transition-colors hover:text-fg-2"
          >
            Seats on Fleet
          </Link>
          {live}
        </div>
      }
    />
  );
}

// ── Connect an agent ──────────────────────────────────────────────────────────

const CONNECT_IDS = ["claude", "grok", "cursor", "any"] as const;

function anySnippet(url: string): { code: string; note: string } {
  return {
    code:
      `curl -s ${url} \\\n` +
      `  -H "X-API-Key: $GRAPHBAN_KEY" \\\n` +
      `  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`,
    note: "Any client that speaks MCP over HTTP works. tools/list shows what this key's tiers can see.",
  };
}

function connectSnippet(id: string, url: string): { code: string; note: string; file?: string } {
  if (id === "any") return anySnippet(url);
  const client = CLIENTS.find((c) => c.id === id);
  if (!client) return anySnippet(url);
  const form = client.command ?? client.config;
  if (!form) return anySnippet(url);
  const noteRaw = typeof form.note === "function" ? form.note("project") : form.note;
  return {
    code: form.build(url, "$GRAPHBAN_KEY", "project"),
    note: noteRaw || client.noCommand || "",
    file: form.file,
  };
}

function ConnectAgent() {
  const [active, setActive] = React.useState<(typeof CONNECT_IDS)[number]>("claude");
  const [copied, setCopied] = React.useState(false);
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const url = `${origin}/api/mcp`;
  const snippet = connectSnippet(active, url);

  const copy = React.useCallback(() => {
    void copyText(snippet.code).then((ok) => {
      if (!ok) return;
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [snippet.code]);

  return (
    <section className="overflow-hidden rounded-[12px] border border-line-2 bg-surface-2" data-testid="mcp-connect">
      <div className="flex flex-wrap items-center gap-x-3.5 gap-y-2 border-b border-line px-4 py-3">
        <h2 className="text-lead font-semibold text-fg">Connect an agent</h2>
        <span className="font-mono text-small text-muted">POST /api/mcp · JSON-RPC</span>
        <span className="flex-1" />
        <div className="flex overflow-hidden rounded-lg border border-control" role="group" aria-label="Client">
          {CONNECT_IDS.map((id) => {
            const label = id === "any" ? "Any client" : CLIENTS.find((c) => c.id === id)?.label ?? id;
            const on = id === active;
            return (
              <button
                key={id}
                type="button"
                onClick={() => { setActive(id); setCopied(false); }}
                aria-pressed={on}
                className={cn(
                  "h-7 border-0 px-2.5 text-small",
                  on ? "bg-surface-3 text-fg" : "bg-transparent text-faint hover:text-fg",
                )}
              >
                {label}
              </button>
            );
          })}
        </div>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3">
        <ConnectStep n="1" title="Mint a key">
          Settings → API keys. Project scope for one project; tiers decide which specialist tools the agent can see.{" "}
          <Link to={settingsPath("project/api-keys")} className="text-accent hover:underline">
            Open API keys →
          </Link>
        </ConnectStep>
        <ConnectStep n="2" title="Add the server">
          Run the snippet below on the agent&apos;s machine. The key lives in an env var, never in the prompt.
        </ConnectStep>
        <ConnectStep n="3" title="First call">
          Tell the agent to call get_context, then register_agent. A seat from Fleet goes in the prompt.
        </ConnectStep>
      </div>
      <div className="relative border-t border-line bg-[#0a0d10]">
        {snippet.file && (
          <div className="px-4 pt-2 font-mono text-meta text-faint">{snippet.file}</div>
        )}
        <pre className="overflow-x-auto p-3.5 pr-20 font-mono text-[12px] leading-relaxed text-fg-2">
          {snippet.code}
        </pre>
        <button
          type="button"
          onClick={copy}
          className="absolute right-3 top-2.5 h-[26px] rounded-md border border-control bg-surface-2 px-2.5 text-small text-muted hover:border-control-hover hover:text-fg"
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      {snippet.note && (
        <p className="border-t border-line px-4 py-2.5 text-small leading-relaxed text-faint">
          {snippet.note}
        </p>
      )}
    </section>
  );
}

function ConnectStep({ n, title, children }: { n: string; title: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5 border-line p-3.5 sm:border-r sm:last:border-r-0">
      <div className="flex items-center gap-2">
        <span className="flex h-5 w-5 items-center justify-center rounded-md border border-accent/40 bg-accent/10 font-mono text-meta text-accent">
          {n}
        </span>
        <span className="text-body font-semibold text-fg">{title}</span>
      </div>
      <p className="text-[12px] leading-relaxed text-muted">{children}</p>
    </div>
  );
}

// ── Loop by role ──────────────────────────────────────────────────────────────

function LoopByRole({
  tools, openName, onSelect,
}: {
  tools: McpToolInfo[];
  openName: string | null;
  onSelect: (name: string) => void;
}) {
  const [role, setRole] = React.useState<RoleId>("all-in-one");
  const loop = ROLE_LOOPS.find((r) => r.role === role) ?? ROLE_LOOPS[0];
  const present = new Set(tools.map((t) => t.name));

  return (
    <section data-testid="mcp-loop">
      <div className="mb-3 flex flex-wrap items-end gap-x-3.5 gap-y-2">
        <div className="min-w-0 flex-1">
          <h2 className="text-lead font-semibold text-fg">The loop, by role</h2>
          <p className="mt-0.5 text-[12px] text-muted">
            The order of calls a well-behaved agent makes. This is what the seat prompt on Fleet teaches.
          </p>
        </div>
        <div className="flex flex-wrap gap-1">
          {ROLE_LOOPS.map((r) => {
            const on = r.role === role;
            return (
              <button
                key={r.role}
                type="button"
                onClick={() => setRole(r.role)}
                aria-pressed={on}
                className={cn(
                  "rounded-lg border px-2.5 py-1 text-[12px] transition-colors",
                  on
                    ? "border-accent/50 bg-surface-3 text-fg"
                    : "border-control bg-transparent text-muted hover:text-fg",
                )}
              >
                {r.role}
              </button>
            );
          })}
        </div>
      </div>
      <p className="mb-2.5 max-w-[92ch] text-[12px] leading-relaxed text-fg-2">{loop.intro}</p>
      <ol className="grid list-none grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-2 p-0">
        {loop.steps.map((step, i) => {
          const on = step.tool === openName;
          const missing = !present.has(step.tool);
          return (
            <li key={step.tool}>
              <button
                type="button"
                onClick={() => onSelect(step.tool)}
                disabled={missing}
                className={cn(
                  "flex h-full w-full flex-col gap-1.5 rounded-[11px] border bg-surface-2 px-3 py-2.5 text-left",
                  on ? "border-accent/50" : "border-control hover:border-control-hover",
                  missing && "opacity-50",
                )}
              >
                <span className="flex items-center gap-2">
                  <span className="font-mono text-meta text-faint">{String(i + 1).padStart(2, "0")}</span>
                  <span className={cn("font-mono text-[12px]", on ? "text-accent" : "text-fg-2")}>
                    {step.tool}
                  </span>
                </span>
                <span className="text-small leading-relaxed text-muted">{step.why}</span>
              </button>
            </li>
          );
        })}
      </ol>
      {loop.avoid && (
        <p className="mt-2.5 text-small leading-relaxed text-[color:var(--color-st-review)]" data-testid="mcp-avoid">
          {loop.avoid}
        </p>
      )}
    </section>
  );
}

// ── Tool reference ────────────────────────────────────────────────────────────

function ToolReference({
  tools, live, openName, onSelect,
}: {
  tools: McpToolInfo[];
  live: number;
  openName: string | null;
  onSelect: (name: string) => void;
}) {
  const [query, setQuery] = React.useState("");
  const enriched = React.useMemo(() => tools.map(enrich), [tools]);

  const grouped = React.useMemo(() => {
    const q = query.toLowerCase().trim();
    const filtered = q
      ? enriched.filter((t) =>
          (t.info.name + t.short + t.when + t.info.description + t.info.params.join(" "))
            .toLowerCase()
            .includes(q),
        )
      : enriched;
    return GROUPS
      .map((g) => ({ ...g, tools: filtered.filter((t) => t.group === g.id) }))
      .filter((g) => g.tools.length > 0);
  }, [enriched, query]);

  const visible = grouped.flatMap((g) => g.tools);
  const selected =
    visible.find((t) => t.info.name === openName)
    ?? visible[0]
    ?? null;

  const coreCount = enriched.filter((t) => t.tier === "core").length;

  return (
    <section className="flex flex-wrap items-start gap-3.5" data-testid="mcp-reference">
      <div className="min-w-0 flex-1 basis-[460px] overflow-hidden rounded-[12px] border border-line-2 bg-surface-2">
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2.5">
          <h2 className="text-lead font-semibold text-fg">Reference</h2>
          <div className="relative min-w-[160px] flex-1">
            <Search
              size={13}
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted opacity-55"
              aria-hidden
            />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Find a tool or what it does…"
              aria-label="Search tools"
              className="h-[30px] w-full rounded-lg border border-control bg-surface py-0 pl-8 pr-8 text-[12px] text-fg placeholder:text-faint focus:border-control-hover focus:outline-none"
            />
            {query && (
              <button
                type="button"
                onClick={() => setQuery("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-faint hover:text-fg"
                aria-label="Clear search"
              >
                ×
              </button>
            )}
          </div>
        </div>
        {grouped.length === 0 ? (
          <div className="px-4 py-8 text-center text-body text-muted" data-testid="mcp-none">
            No tool matches. Try what you want to do, e.g. &quot;review&quot; or &quot;files&quot;.
          </div>
        ) : (
          grouped.map((g) => (
            <div key={g.id}>
              <div className="flex items-baseline gap-2 border-b border-line bg-surface px-3 py-2">
                <span className="font-mono text-meta uppercase tracking-wide text-muted">{g.label}</span>
                <span className="text-small text-faint">{g.hint}</span>
              </div>
              <div className="divide-y divide-line">
              {g.tools.map((t) => {
                const on = selected?.info.name === t.info.name;
                const tone = TIER_TONE[t.tier];
                return (
                  <button
                    key={t.info.name}
                    type="button"
                    onClick={() => onSelect(t.info.name)}
                    aria-pressed={on}
                    className={cn(
                      "grid w-full grid-cols-[minmax(0,150px)_minmax(0,1fr)_auto] items-baseline gap-2.5 px-3 py-2 text-left",
                      on ? "bg-surface-3 shadow-[inset_2px_0_0_var(--color-accent)]" : "hover:bg-surface-3",
                    )}
                  >
                    <span className={cn("truncate font-mono text-[12px]", on ? "text-accent" : "text-fg-2")}>
                      {t.info.name}
                    </span>
                    <span className="truncate text-[12px] text-muted">{t.short}</span>
                    <span className={cn("rounded border px-1.5 py-px font-mono text-micro uppercase tracking-wide", tone.border, tone.color)}>
                      {t.tier}
                    </span>
                  </button>
                );
              })}
              </div>
            </div>
          ))
        )}
        <div className="px-3 py-2.5 text-small text-faint">
          {tools.length} of {live} live tools shown · {coreCount} core ship to every key; the rest appear by tier
        </div>
      </div>
      {selected && <ToolDetail tool={selected} all={enriched} onSelect={onSelect} />}
    </section>
  );
}

function ToolDetail({
  tool, all, onSelect,
}: {
  tool: EnrichedTool;
  all: EnrichedTool[];
  onSelect: (name: string) => void;
}) {
  const tone = TIER_TONE[tool.tier];
  const params = tool.info.param_details ?? [];
  const knownNext = tool.next.filter((n) => all.some((t) => t.info.name === n));
  return (
    <aside
      className="sticky top-3 min-w-[300px] max-w-[460px] flex-1 basis-[360px] rounded-[12px] border border-line-2 bg-surface-2"
      data-testid="mcp-detail"
    >
      <div className="flex flex-col gap-2 border-b border-line px-4 py-3.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-lead text-accent">{tool.info.name}</span>
          <span className={cn("rounded border px-1.5 py-px font-mono text-micro uppercase tracking-wide", tone.border, tone.color)}>
            {tool.tier}
          </span>
          <span className="ml-auto font-mono text-meta text-faint">
            {fmtCalls(tool.info.calls)} calls
          </span>
        </div>
        <p className="text-body leading-relaxed text-fg-2">{tool.when}</p>
      </div>
      <div className="flex flex-col gap-1.5 border-b border-line px-4 py-3">
        <span className="font-mono text-meta uppercase tracking-wide text-faint">Example</span>
        <pre className="overflow-x-auto rounded-[9px] border border-line bg-surface px-3 py-2.5 font-mono text-small leading-relaxed text-fg-2">
          {tool.example}
        </pre>
      </div>
      <div className="flex flex-col gap-1.5 border-b border-line px-4 py-3">
        <span className="font-mono text-meta uppercase tracking-wide text-faint">Params</span>
        {params.length === 0 ? (
          <span className="font-mono text-small text-faint">No parameters — this tool takes no input.</span>
        ) : (
          params.map((p) => <ParamRow key={p.name} param={p} />)
        )}
        {tool.returns && (
          <>
            <span className="mt-1 font-mono text-meta uppercase tracking-wide text-faint">Returns</span>
            <span className="text-small leading-relaxed text-muted">{tool.returns}</span>
          </>
        )}
      </div>
      <div className="flex flex-col gap-2 px-4 py-3">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="mr-1 font-mono text-meta uppercase tracking-wide text-faint">Roles</span>
          {tool.roles.map((r) => {
            const t = ROLE_TONE[r];
            return (
              <span key={r} className={cn("rounded-md border px-1.5 py-px font-mono text-meta", t.border, t.color)}>
                {r}
              </span>
            );
          })}
        </div>
        {tool.refused && (
          <p className="text-small leading-relaxed text-[color:var(--color-st-blocked)]">
            Refused when {tool.refused}
          </p>
        )}
        {knownNext.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 text-small text-faint">
            Usually followed by
            {knownNext.map((n) => (
              <button
                key={n}
                type="button"
                onClick={() => onSelect(n)}
                className="border-0 bg-transparent px-0.5 font-mono text-small text-accent hover:underline"
              >
                {n}
              </button>
            ))}
          </div>
        )}
      </div>
    </aside>
  );
}

function ParamRow({ param }: { param: McpParamDetail }) {
  return (
    <div className="grid grid-cols-[minmax(0,120px)_minmax(0,1fr)] gap-2.5 text-small">
      <span className="font-mono text-fg-2">
        {param.name}
        {param.required && <span className="text-[color:var(--color-st-blocked)]"> *</span>}
      </span>
      <span className="text-muted">
        {param.description || param.type || "—"}
        {param.enum && param.enum.length > 0 && (
          <span className="mt-0.5 flex flex-wrap gap-1">
            {param.enum.map((v) => (
              <span key={v} className="rounded border border-line-2 bg-surface px-1 font-mono text-micro text-muted-2">
                {v}
              </span>
            ))}
          </span>
        )}
      </span>
    </div>
  );
}
