import { useMemo, useState } from "react";
import { Download } from "lucide-react";
import { Link } from "react-router-dom";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import {
  FETCH_FAILED,
  KpiGridSkeleton,
  PlannerEmpty,
  PlannerError,
  TableSkeleton,
} from "@/components/planner/PlannerStates";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";
import { api } from "@/lib/api";
import { useUsage } from "@/lib/queries";
import { lastProjectTag, projectPath } from "@/lib/routes";
import type {
  UsageKpi,
  UsageLimitRow,
  UsageModelRow,
  UsageModelUsage,
  UsageProjectRow,
} from "@/lib/types";

const RANGES = [7, 30, 90] as const;

/** PRD-47 S14's footnote, verbatim — D3: the design's copy is the mechanism, not decoration. */
const MODEL_USAGE_FOOTNOTE =
  "Cost is estimated from list prices of the tokens agents reported. Local models (gbagent) show $0.";

/**
 * PRD-47 S14 — deployment-wide usage. One aggregate fetch. Undeclared limits render as
 * undeclared, never as a %.
 *
 * The model-usage panel is fed by PRD-38 attempt records, and keeps GRPH-980's objection —
 * a cost column over tokens nobody reports is a table of zeroes — by never printing one.
 * An unmeasured pair reads `not reported` and an unpriced one `not priced`; `$0` is reserved
 * for compute that really is free.
 */
export function UsageView() {
  const [range, setRange] = useState<number>(30);
  const [hiddenProjects, setHiddenProjects] = useState<Set<string>>(new Set());
  const q = useUsage(range);
  const { data, isLoading, isError, refetch } = q;

  const visibleProjects = useMemo(
    () => (data?.chart.projects ?? []).filter((p) => !hiddenProjects.has(p.id)),
    [data, hiddenProjects],
  );

  if (isError && !data) {
    return (
      <div className="flex h-full min-h-0 flex-col" data-testid="usage-error">
        <PlaceHeader viewName="Usage" purpose="Deployment-wide MCP calls, agents, and limits." />
        <PlannerError message={FETCH_FAILED} onRetry={() => void refetch()} />
      </div>
    );
  }

  if (isLoading || !data) {
    return (
      <div className="flex h-full min-h-0 flex-col" data-testid="usage-loading">
        <PlaceHeader viewName="Usage" purpose="Deployment-wide MCP calls, agents, and limits." />
        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          <KpiGridSkeleton tiles={5} />
          <div className="mt-4">
            <TableSkeleton rows={6} columns={5} />
          </div>
        </div>
      </div>
    );
  }

  const peak = Math.max(1, ...data.chart.buckets.map((b) => b.value));

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="usage-view">
      <PlaceHeader
        viewName="Usage"
        purpose="Deployment-wide MCP calls, agents, projects, and license limits."
        action={
          <div className="flex items-center gap-2">
            {RANGES.map((d) => (
              <button
                key={d}
                type="button"
                onClick={() => setRange(d)}
                className={cn(
                  "rounded-md border px-2.5 py-1 font-mono text-[10.5px] transition-colors",
                  range === d
                    ? "border-control-hover bg-surface-3 text-fg"
                    : "border-control text-muted hover:border-control-hover",
                )}
              >
                {d}d
              </button>
            ))}
            <Button
              size="sm"
              variant="outline"
              onClick={() => void downloadCsv(range)}
              className="gap-1.5"
            >
              <Download size={14} />
              CSV
            </Button>
          </div>
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        <IdentityStrip identity={data.identity} />

        <section className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          {data.kpis.map((kpi) => (
            <KpiCard key={kpi.id} kpi={kpi} />
          ))}
        </section>

        <section className="mt-5 rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
          <h2 className="mb-2 text-body text-fg">MCP calls per day</h2>
          {data.chart.note && (
            <p className="mb-2 text-[11.5px] leading-snug text-st-review">{data.chart.note}</p>
          )}
          <div className="flex h-24 items-end gap-px" role="img" aria-label="MCP calls per day">
            {data.chart.buckets.map((b, i) => (
              <div
                key={i}
                className="relative flex min-w-0 flex-1 flex-col justify-end"
                title={`${b.value} calls`}
              >
                {visibleProjects.map((p, pi) => {
                  const v = p.series[i]?.value ?? 0;
                  if (!v) return null;
                  const projectPeak = Math.max(
                    1,
                    ...visibleProjects.flatMap((pp) => pp.series.map((s) => s.value)),
                  );
                  return (
                    <span
                      key={p.id}
                      className="w-full"
                      style={{
                        height: `${(v / projectPeak) * 100}%`,
                        backgroundColor: projectColor(pi),
                      }}
                    />
                  );
                })}
                {!visibleProjects.length && b.value > 0 && (
                  <span className="w-full bg-accent" style={{ height: `${(b.value / peak) * 100}%` }} />
                )}
              </div>
            ))}
          </div>
          <div className="mt-2 flex flex-wrap gap-2">
            {data.chart.projects.map((p, i) => {
              const hidden = hiddenProjects.has(p.id);
              return (
                <button
                  key={p.id}
                  type="button"
                  onClick={() =>
                    setHiddenProjects((s) => {
                      const n = new Set(s);
                      if (n.has(p.id)) n.delete(p.id);
                      else n.add(p.id);
                      return n;
                    })
                  }
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-small",
                    hidden ? "border-control text-faint opacity-60" : "border-control-hover text-muted",
                  )}
                >
                  <span
                    className="h-2 w-2 rounded-[2px]"
                    style={{ backgroundColor: projectColor(i) }}
                    aria-hidden
                  />
                  {p.tag}
                </button>
              );
            })}
          </div>
        </section>

        <section className="mt-5 overflow-hidden rounded-[10px] border border-line-2 bg-surface-2">
          <div className="border-b border-line bg-surface px-4 py-2.5 text-body text-fg">
            By project
          </div>
          <ProjectTable rows={data.by_project} />
        </section>

        <section className="mt-5 overflow-hidden rounded-[10px] border border-line-2 bg-surface-2">
          <div className="border-b border-line bg-surface px-4 py-2.5 text-body text-fg">
            License limits
          </div>
          <LimitsTable limits={data.limits} note={data.on_pace_note} />
        </section>

        <section
          className="mt-5 overflow-hidden rounded-[10px] border border-line-2 bg-surface-2"
          data-testid="usage-model-usage"
        >
          <div className="flex items-center justify-between gap-3 border-b border-line bg-surface px-4 py-2.5">
            <span className="text-body text-fg">Model usage</span>
            <HarnessLink tags={data.by_project.map((p) => p.tag)} />
          </div>
          <ModelUsagePanel usage={data.model_usage} />
        </section>

        <section className="mt-5 overflow-hidden rounded-[10px] border border-line-2 bg-surface-2">
          <div className="border-b border-line bg-surface px-4 py-2.5 text-body text-fg">
            Busiest API keys
          </div>
          {data.busiest_keys.length === 0 ? (
            <p className="p-4 text-body text-muted">
              No MCP calls recorded in this range — not a quiet deployment, a window with no telemetry.
            </p>
          ) : (
            <table className="w-full text-left text-[12px]">
              <thead>
                <tr className="border-b border-line text-faint">
                  <th className="px-4 py-2 font-normal">Key</th>
                  <th className="px-4 py-2 font-normal">Owner</th>
                  <th className="px-4 py-2 font-normal text-right">Calls</th>
                  <th className="px-4 py-2 font-normal">Last seen</th>
                </tr>
              </thead>
              <tbody>
                {data.busiest_keys.map((k) => (
                  <tr key={k.id} className="border-b border-line/60">
                    <td className="px-4 py-2 font-mono text-small">{k.name}</td>
                    <td className="px-4 py-2 text-muted">{k.owner}</td>
                    <td className="px-4 py-2 text-right font-mono">{k.calls.toLocaleString()}</td>
                    <td className="px-4 py-2 text-faint">
                      {k.last_seen ? new Date(k.last_seen).toLocaleString() : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </div>
  );
}

function IdentityStrip({
  identity,
}: {
  identity: { mode: string; host: string; version: string; git_sha: string; license: string };
}) {
  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-[11.5px] text-muted"
      data-testid="usage-identity"
    >
      <span className="capitalize text-fg">{identity.mode}</span>
      <span>{identity.host || window.location.host}</span>
      <span className="font-mono text-[10.5px]">
        {identity.version}
        {identity.git_sha ? ` · ${identity.git_sha.slice(0, 7)}` : ""}
      </span>
      <span>License: {identity.license}</span>
    </div>
  );
}

function KpiCard({ kpi }: { kpi: UsageKpi }) {
  const sparkPeak = Math.max(1, ...kpi.sparkline.map((b) => b.value));
  return (
    <div className="rounded-[10px] border border-line-2 bg-surface-2 px-3 py-2.5">
      <div className="text-small text-faint">{kpi.label}</div>
      <div className="mt-1 flex items-baseline gap-2">
        <span className="font-mono text-title leading-none text-fg">
          {kpi.value == null ? "—" : kpi.value.toLocaleString()}
        </span>
        {kpi.delta != null && (
          <span className={cn("font-mono text-[10.5px]", kpi.delta >= 0 ? "text-st-done" : "text-st-blocked")}>
            {kpi.delta >= 0 ? "+" : ""}
            {kpi.delta.toLocaleString()}
          </span>
        )}
      </div>
      <div className="mt-2 flex h-6 items-end gap-px">
        {kpi.sparkline.map((b, i) => (
          <span
            key={i}
            className="min-w-0 flex-1 rounded-[1px] bg-accent/70"
            style={{ height: `${(b.value / sparkPeak) * 100}%` }}
          />
        ))}
      </div>
    </div>
  );
}

function ProjectTable({ rows }: { rows: UsageProjectRow[] }) {
  const [sort, setSort] = useState<"calls" | "agents" | "shards" | "done">("calls");
  const sorted = [...rows].sort((a, b) => b[sort] - a[sort]);
  const maxCalls = Math.max(1, ...rows.map((r) => r.calls));

  if (!rows.length) {
    return (
      <p className="p-4 text-body text-muted">
        No projects you can read — not an empty deployment, a scope with nothing in it.
      </p>
    );
  }

  return (
    <table className="w-full text-left text-[12px]">
      <thead>
        <tr className="border-b border-line text-faint">
          <th className="px-4 py-2 font-normal">Project</th>
          {(["calls", "agents", "shards", "done"] as const).map((col) => (
            <th key={col} className="px-4 py-2 font-normal text-right">
              <button type="button" className="hover:text-fg" onClick={() => setSort(col)}>
                {col}
              </button>
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {sorted.map((r) => (
          <tr key={r.id} className="border-b border-line/60">
            <td className="px-4 py-2">
              <span className="font-mono text-small text-accent">{r.tag}</span>
              <span className="ml-2 text-muted">{r.name}</span>
            </td>
            <td className="px-4 py-2 text-right">
              <div className="flex items-center justify-end gap-2">
                <span className="h-1 w-16 overflow-hidden rounded-sm bg-line">
                  <span className="block h-full bg-accent" style={{ width: `${(r.calls / maxCalls) * 100}%` }} />
                </span>
                <span className="w-10 font-mono">{r.calls}</span>
              </div>
            </td>
            <td className="px-4 py-2 text-right font-mono">{r.agents}</td>
            <td className="px-4 py-2 text-right font-mono">{r.shards}</td>
            <td className="px-4 py-2 text-right font-mono">{r.done}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function LimitsTable({ limits, note }: { limits: UsageLimitRow[]; note: string | null }) {
  return (
    <div className="p-4">
      {limits.map((row) => (
        <div key={row.id} className="flex items-center gap-3 py-2" data-testid={`limit-${row.id}`}>
          <span className="w-[140px] shrink-0 text-[11.5px] text-faint">{row.label}</span>
          {row.declared && row.limit != null && row.used != null ? (
            <>
              <span className="h-1 min-w-0 flex-1 overflow-hidden rounded-sm bg-line">
                <span
                  className="block h-full bg-st-done"
                  style={{ width: `${Math.min(100, Math.round((row.used / row.limit) * 100))}%` }}
                />
              </span>
              <span className="w-[150px] shrink-0 text-right font-mono text-[10.5px] text-muted">
                {row.used.toLocaleString()} / {row.limit.toLocaleString()}
              </span>
            </>
          ) : (
            <span className="text-[11.5px] text-st-review">Limit undeclared on this deployment</span>
          )}
        </div>
      ))}
      {note && <p className="mt-2 text-small leading-relaxed text-faint">{note}</p>}
    </div>
  );
}

/**
 * Where "Performance on Harness" can go. Usage is deployment-wide and there is no ambient
 * project (PRD-21 D1.1), so this resolves the last-used tag and only when it is one this
 * caller can read. Otherwise the link is not drawn: pointing at a project that 404s or 403s
 * is worse than no link, and guessing one would invent a scope the page does not have.
 */
function HarnessLink({ tags }: { tags: string[] }) {
  const last = lastProjectTag();
  const tag = last && tags.includes(last) ? last : tags[0];
  if (!tag) return null;
  return (
    <Link
      to={projectPath(tag, "harness")}
      className="text-[11.5px] text-accent hover:underline"
      data-testid="usage-models-harness-link"
    >
      Performance on Harness →
    </Link>
  );
}

function ModelUsagePanel({ usage }: { usage: UsageModelUsage }) {
  if (!usage.rows.length) {
    return (
      <PlannerEmpty
        title="No attempts in this window"
        description="Nothing finished a delegation here, so there is no model spend to price. That is a window with no attempts in it — not a deployment whose agents cost nothing."
      />
    );
  }
  const peak = Math.max(1, ...usage.rows.map((r) => r.tokens ?? 0));
  return (
    <div data-testid="usage-models">
      <table className="w-full text-left text-[12px]">
        <thead>
          <tr className="border-b border-line text-faint">
            <th className="px-4 py-2 font-normal">Harness · Model</th>
            <th className="px-4 py-2 font-normal text-right">Spawns</th>
            <th className="px-4 py-2 font-normal text-right">Tokens</th>
            <th className="px-4 py-2 font-normal text-right">Est. cost</th>
          </tr>
        </thead>
        <tbody>
          {usage.rows.map((row) => (
            <ModelRow key={`${row.vendor}:${row.model}`} row={row} peak={peak} />
          ))}
        </tbody>
      </table>
      <div className="px-4 py-2.5">
        {usage.note && (
          <p className="text-small leading-relaxed text-st-review" data-testid="usage-models-note">
            {usage.note}
          </p>
        )}
        <p className={cn("text-small leading-relaxed text-faint", usage.note && "mt-1")}>
          {MODEL_USAGE_FOOTNOTE}
        </p>
      </div>
    </div>
  );
}

function ModelRow({ row, peak }: { row: UsageModelRow; peak: number }) {
  // A sum over the attempts that reported, beside the count it rests on. Without the count a
  // partial numerator reads as a total — the failure `harness.COST_COVERAGE` exists to stop.
  const partial = row.tokens_reported < row.spawns;
  return (
    <tr className="border-b border-line/60" data-testid={`usage-model-row-${row.vendor}`}>
      <td className="px-4 py-2">
        <span className="text-muted">{row.vendor}</span>
        <span className="ml-2 font-mono text-small text-accent">{row.model}</span>
      </td>
      <td className="px-4 py-2 text-right font-mono">{row.spawns.toLocaleString()}</td>
      <td className="px-4 py-2 text-right">
        {row.tokens == null ? (
          <NotReported
            label="not reported"
            detail={`${row.spawns.toLocaleString()} spawns in this window, none carried a token count.`}
          />
        ) : (
          <div className="flex items-center justify-end gap-2">
            <span className="h-1 w-16 overflow-hidden rounded-sm bg-line">
              <span
                className="block h-full bg-accent"
                style={{ width: `${(row.tokens / peak) * 100}%` }}
              />
            </span>
            <span className="w-20 font-mono">{row.tokens.toLocaleString()}</span>
            {partial && (
              <span className="w-[86px] text-right text-[10.5px] text-faint-2">
                {row.tokens_reported}/{row.spawns} reported
              </span>
            )}
          </div>
        )}
      </td>
      <td
        className="px-4 py-2 text-right"
        title={
          row.cost_usd == null
            ? "No list price matches this model, so there is no estimate to show."
            : partial
              ? `Prices only the ${row.tokens_reported} of ${row.spawns} attempts that reported tokens.`
              : undefined
        }
      >
        {row.cost_usd == null ? (
          <span className="text-[11.5px] text-st-review">not priced</span>
        ) : (
          <span className="font-mono">${row.cost_usd.toFixed(2)}</span>
        )}
      </td>
    </tr>
  );
}

/** The seam, mirroring Activity's `NotRecorded`: a labelled gap in the shape of the thing that
 *  would go there. Not a blank and not a zero — a zero in a token column claims the harness ran
 *  and used nothing, which is the one reading this page must not offer. */
function NotReported({ label, detail }: { label: string; detail: string }) {
  return (
    <div className="ml-auto inline-block max-w-[240px] rounded-[8px] border border-dashed border-line-2 px-2.5 py-1.5 text-left">
      <p className="text-[11.5px] text-muted">{label}</p>
      <p className="mt-0.5 text-small leading-snug text-faint-2">Not reported. {detail}</p>
    </div>
  );
}

const PALETTE = ["#c6f24e", "#7dd3fc", "#f472b6", "#fb923c", "#a78bfa", "#34d399"];

function projectColor(i: number): string {
  return PALETTE[i % PALETTE.length];
}

async function downloadCsv(rangeDays: number) {
  const text = await api.usageCsv(rangeDays);
  const blob = new Blob([text], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "usage-by-project.csv";
  a.click();
  URL.revokeObjectURL(url);
}
