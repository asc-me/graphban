import { Link } from "react-router-dom";

import { useMcpTools } from "@/lib/queries";
import { settingsPath } from "@/lib/routes";
import type { McpToolInfo } from "@/lib/types";

export function McpToolsView() {
  const { data, isLoading, isError } = useMcpTools();

  if (isLoading && !data) {
    return <div className="flex h-full items-center justify-center text-[13px] text-muted">Loading…</div>;
  }
  if (isError && !data) {
    return (
      <div className="p-6 text-[13px] text-st-blocked">
        MCP catalog unavailable — the tool list could not be fetched.
      </div>
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
      <div className="flex flex-none items-center gap-4 border-b border-line px-5 py-4">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight">MCP Tools</h1>
          <p className="mt-0.5 text-[12.5px] text-muted">
            The Model Context Protocol surface agents call. Same code path as the web app.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-3">
          {/* This page is the catalog. The key that authenticates lives on Settings →
              API keys — the page people open this one looking for. Named as a question
              because the destination is the answer. Same words as AI Providers:
              "a key" could be an Anthropic secret. */}
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
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {data.tools.map((t) => (
            <ToolCard key={t.name} tool={t} />
          ))}
        </div>
      </div>
    </div>
  );
}

function fmt(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function ToolCard({ tool }: { tool: McpToolInfo }) {
  return (
    <div className="rounded-[12px] border border-line-2 bg-surface-2 p-3.5 transition-colors hover:border-line-hover">
      <div className="mb-1.5 flex items-center gap-2">
        <span className="font-mono text-[12.5px] text-accent">{tool.name}</span>
        <span className="rounded border border-[#1c2620] bg-[rgba(95,208,122,0.06)] px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide text-st-done">
          live
        </span>
        <span className="ml-auto font-mono text-[10px] text-faint">{fmt(tool.calls)} calls</span>
      </div>
      <p className="mb-2.5 text-[12px] leading-relaxed text-muted">{tool.description}</p>
      <div className="flex flex-wrap gap-1">
        {tool.params.map((p) => (
          <span key={p} className="rounded-md border border-line-2 bg-surface px-1.5 py-0.5 font-mono text-[10px] text-muted-2">
            {p}
          </span>
        ))}
      </div>
    </div>
  );
}
