import { useState } from "react";
import { Link } from "react-router-dom";
import { Bot, Boxes, Brain, CircleDot, FileText, Plug, TriangleAlert, X } from "lucide-react";

import { useProjectCtx } from "@/features/ProjectContext";
import { STATUS_META, STATUS_ORDER, TYPE_META } from "@/lib/meta";
import { useDashboard } from "@/lib/queries";
import type { DashboardData, RequestType } from "@/lib/types";

export function DashboardView() {
  const { activeId } = useProjectCtx();
  const { data, isLoading } = useDashboard(activeId);

  if (isLoading || !data) {
    return <div className="flex h-full items-center justify-center text-[13px] text-muted">Loading…</div>;
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex-none border-b border-line px-5 py-4">
        <h1 className="text-[18px] font-semibold tracking-tight">Dashboard</h1>
        <p className="mt-0.5 text-[12.5px] text-muted">Project health at a glance — items, memory, requests, and MCP activity.</p>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        <AgentLoopInfo />

        {/* KPI tiles (hero numbers — no plot, no hover) */}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Kpi icon={<Boxes size={15} />} label="Items" value={data.items_total} />
          <Kpi icon={<CircleDot size={15} />} label="In progress" value={data.in_progress_count} accent="#c6f24e" />
          <Kpi icon={<TriangleAlert size={15} />} label="Blocked" value={data.blocked_count} accent="#ff6b6b" />
          <Kpi icon={<Brain size={15} />} label="Memory shards" value={data.shard_count} accent="#a78bfa" />
          <Kpi icon={<FileText size={15} />} label="PRDs" value={data.prd_count} />
          <Kpi icon={<Plug size={15} />} label="MCP calls" value={fmt(data.mcp_calls)} accent="#5fd07a" />
        </div>

        <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-[1.4fr_1fr]">
          <div className="space-y-6">
            <Panel title="Item status distribution">
              <StatusBar data={data} />
            </Panel>
            <Panel title="Requests by type">
              <TypeBars data={data} />
            </Panel>
          </div>

          <Panel title="Recent activity">
            <div className="space-y-1.5">
              {data.recent_items.map((it) => (
                <div key={it.id} className="flex items-center gap-2.5 rounded-lg border border-line-2 bg-surface-2 px-3 py-2">
                  <span className="h-1.5 w-1.5 flex-none rounded-full" style={{ background: STATUS_META[it.status].color }} />
                  <span className="w-[46px] flex-none font-mono text-[10px] text-faint">{it.id}</span>
                  <span className="min-w-0 flex-1 truncate text-[12.5px] text-fg-2">{it.title}</span>
                  <span className="flex-none font-mono text-[10px] text-faint-2">{it.date}</span>
                </div>
              ))}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}

const AGENT_INFO_DISMISSED_KEY = "al.dashboard.agentLoopInfoDismissed";

function AgentLoopInfo() {
  const [dismissed, setDismissed] = useState(() => localStorage.getItem(AGENT_INFO_DISMISSED_KEY) === "1");
  if (dismissed) return null;
  return (
    <div className="relative mb-6 rounded-[14px] border border-line-2 bg-surface-2 p-4">
      <button
        type="button"
        aria-label="Dismiss"
        className="absolute right-3 top-3 text-faint transition-colors hover:text-fg-2"
        onClick={() => {
          localStorage.setItem(AGENT_INFO_DISMISSED_KEY, "1");
          setDismissed(true);
        }}
      >
        <X size={14} />
      </button>
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex-none text-[#c6f24e]">
          <Bot size={16} />
        </span>
        <div className="min-w-0">
          <div className="text-[13px] font-semibold text-fg">Put agents on the backlog</div>
          <p className="mt-1 text-[12.5px] leading-relaxed text-muted">
            Any MCP-connected agent can work the tracker as a queue:{" "}
            <code className="font-mono text-[11px] text-fg-2">claim_cluster → work → update_item → review</code>. Claims
            are atomic and a cluster reserves the files it touches, so parallel agents never collide. Finished work goes
            to <code className="font-mono text-[11px] text-fg-2">review</code>, and any other agent picks it up with{" "}
            <code className="font-mono text-[11px] text-fg-2">claim_review</code> — no agent can sign off its own.
          </p>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[12px]">
            <Link to="/settings" className="text-[#7ca2ff] transition-colors hover:text-fg">
              Connect an agent →
            </Link>
            <Link to="/mcp-tools" className="text-[#7ca2ff] transition-colors hover:text-fg">
              Browse MCP tools →
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}

function fmt(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function Kpi({ icon, label, value, accent }: { icon: React.ReactNode; label: string; value: number | string; accent?: string }) {
  return (
    <div className="rounded-[12px] border border-line-2 bg-surface-2 p-3.5">
      <div className="mb-2 flex items-center gap-1.5 text-muted" style={{ color: accent }}>
        {icon}
      </div>
      <div className="text-[22px] font-semibold leading-none tracking-tight text-fg">{value}</div>
      <div className="mt-1.5 text-[11.5px] text-muted">{label}</div>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-[14px] border border-line-2 bg-surface/40 p-4">
      <div className="mb-3 font-mono text-[10px] uppercase tracking-wide text-faint">{title}</div>
      {children}
    </div>
  );
}

function StatusBar({ data }: { data: DashboardData }) {
  const total = Math.max(1, data.items_total);
  const present = STATUS_ORDER.filter((s) => (data.items_by_status[s] ?? 0) > 0);
  return (
    <div>
      {/* Segmented bar: rounded data-ends, 2px surface gaps between fills */}
      <div className="flex h-5 gap-[2px]">
        {present.map((s) => {
          const count = data.items_by_status[s] ?? 0;
          return (
            <div
              key={s}
              className="h-full rounded-[3px]"
              style={{ background: STATUS_META[s].color, width: `${(count / total) * 100}%`, minWidth: 8 }}
              title={`${STATUS_META[s].label}: ${count}`}
            />
          );
        })}
      </div>
      {/* Legend — identity via label+count, never color alone */}
      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5">
        {STATUS_ORDER.map((s) => (
          <div key={s} className="flex items-center gap-1.5 text-[12px]">
            <span className="h-2 w-2 rounded-[2px]" style={{ background: STATUS_META[s].color }} />
            <span className="text-muted">{STATUS_META[s].label}</span>
            <span className="font-mono text-[11px] text-fg-2">{data.items_by_status[s] ?? 0}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function TypeBars({ data }: { data: DashboardData }) {
  const max = Math.max(1, ...Object.values(data.requests_by_type));
  const types = Object.keys(TYPE_META) as RequestType[];
  return (
    <div className="space-y-2">
      {types.map((t) => {
        const count = data.requests_by_type[t] ?? 0;
        return (
          <div key={t} className="flex items-center gap-3">
            <span className="w-[92px] flex-none font-mono text-[10px] uppercase tracking-wide" style={{ color: TYPE_META[t].color }}>
              {TYPE_META[t].label}
            </span>
            <div className="h-2.5 flex-1">
              <div className="h-full rounded-[3px]" style={{ background: TYPE_META[t].color, width: `${(count / max) * 100}%`, minWidth: count ? 6 : 0 }} />
            </div>
            <span className="w-6 flex-none text-right font-mono text-[11px] text-fg-2">{count}</span>
          </div>
        );
      })}
    </div>
  );
}
