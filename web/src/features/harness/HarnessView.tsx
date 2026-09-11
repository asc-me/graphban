import { useState } from "react";
import { AlertTriangle, Play, Scale } from "lucide-react";

import { Recommendations } from "@/features/harness/Recommendations";
import { useProjectCtx } from "@/features/ProjectContext";
import { useHarness, useHarnessProbeCandidates, useStartHarnessProbeRun } from "@/lib/queries";
import type {
  HarnessCell,
  HarnessCost,
  HarnessPoint,
  HarnessReviewCell,
  HarnessSampling,
} from "@/lib/types";

/**
 * PRD-38 PR 2 — how each harness has actually turned out, week by week.
 *
 * The page's whole job is to make a number and its warrant inseparable. Every rate carries
 * its `n`; a rate under the sample floor is drawn grey and says so; a cell whose samples came
 * overwhelmingly from one reason carries a badge saying it is not a fair comparison; and the
 * cost proxy either compares or states in words why it will not. Nothing here recommends
 * anything — that is PR 3 — and nothing here changes a preference.
 */
export function HarnessView() {
  const { activeId } = useProjectCtx();
  const [versions, setVersions] = useState<"current" | "all">("current");
  const { data, isLoading } = useHarness(activeId, { versions });

  if (isLoading || !data) {
    return (
      <div className="flex h-full items-center justify-center text-[13px] text-muted">Loading…</div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="harness-view">
      <div className="flex flex-none items-center gap-4 border-b border-line px-5 py-4">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight">Harness</h1>
          <p className="mt-0.5 text-[12.5px] text-muted">
            How each model has turned out, per capability and size band, over the last{" "}
            {data.window_days} days. A rate under {data.floor} finished attempts is shown grey
            because it is not yet a measurement. Family rollups speak until a leaf clears the
            floor.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <label className="font-mono text-[10.5px] text-faint" htmlFor="harness-versions">
            VERSIONS
          </label>
          <select
            id="harness-versions"
            aria-label="Binary versions"
            data-testid="harness-versions"
            className="rounded-[8px] border border-line-2 bg-surface-2 px-2 py-1 text-[12px]"
            value={versions}
            onChange={(e) => setVersions(e.target.value as "current" | "all")}
          >
            <option value="current">Current only</option>
            <option value="all">Every version</option>
          </select>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {data.cells.length === 0 && !(data.unavailable ?? []).length ? (
          <div className="mx-auto mt-16 max-w-md text-center text-[13px] text-muted">
            Nothing measured yet. A cell appears here once a delegation finishes — one row per
            vendor, model, capability and size band.
          </div>
        ) : (
          <div className="mx-auto flex max-w-4xl flex-col gap-3">
            <Recommendations projectId={activeId} />
            {data.coverage && data.coverage.attempts > 0 && (
              <div
                data-testid="harness-coverage"
                className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-[12.5px] text-muted"
              >
                Coverage {data.coverage.rate === null ? "—" : `${Math.round(data.coverage.rate * 100)}%`}
                {" — "}
                {data.coverage.with_leaf}/{data.coverage.attempts} attempts tagged a leaf.
                Attempts that match none land in <span className="font-mono">other</span>.
              </div>
            )}
            {data.platform === null && data.platform_reason && (
              <div
                data-testid="harness-no-platform"
                className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-[12.5px] text-muted"
              >
                {data.platform_reason}
              </div>
            )}
            {data.below_floor_count > 0 && (
              <div
                data-testid="harness-floor-note"
                className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-[12.5px] text-muted"
              >
                {data.below_floor_count} of {data.cells.length} cells are below the{" "}
                {data.floor}-attempt floor. Their rates are shown because hiding them would read
                as having none, not as having too few.
              </div>
            )}
            <ProbePanel
              projectId={activeId}
              suggestions={data.probe_suggestions ?? []}
              labels={data.capability_set?.labels}
            />
            {(data.review_cells ?? []).map((cell) => (
              <ReviewRow key={`f:${cell.key.vendor}:${cell.key.model}:${cell.key.capability}`} cell={cell} floor={data.floor} />
            ))}
            {(data.unavailable ?? []).map((row) => (
              <div
                key={`${row.vendor}:${row.model}:${row.capability}:${row.reason}`}
                data-testid="harness-unavailable"
                className="rounded-[10px] border border-dashed border-line-2 bg-surface-2 px-3.5 py-3 text-faint"
              >
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span className="font-mono text-[12.5px]">
                    {row.vendor}{row.model ? `:${row.model}` : ""}
                  </span>
                  <span className="font-mono text-[10.5px]">{row.capability}</span>
                  <span data-testid="harness-unavailable-reason" className="font-mono text-[10.5px]">
                    {row.label || row.reason}
                  </span>
                </div>
                <p className="mt-1 text-[12px]">
                  Greyed because it is {row.reason}, not because it is unmeasured.
                  Control: <span className="font-mono">{row.control}</span>
                  {row.drops ? ` · dropped ${row.drops} times` : ""}
                </p>
              </div>
            ))}
            {data.cells.map((cell) => (
              <CellRow key={cellId(cell)} cell={cell} floor={data.floor} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function cellId(cell: HarnessCell): string {
  const k = cell.key;
  return [k.vendor, k.model, k.binary_version, k.capability, k.size_band].join(":");
}

function CellRow({ cell, floor }: { cell: HarnessCell; floor: number }) {
  const k = cell.key;
  const pct = cell.rate === null ? "—" : `${Math.round(cell.rate * 100)}%`;
  return (
    <div
      data-testid="harness-cell"
      className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3"
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-mono text-[12.5px]">
          {k.vendor}
          {k.model ? `:${k.model}` : ""}
        </span>
        <span className="font-mono text-[10.5px] text-faint">
          {cell.label ?? k.capability} · {k.size_band}
          {k.binary_version ? ` · ${k.binary_version}` : ""}
        </span>
        {cell.kind === "family" && (
          <Badge testid="harness-family-label" tone="faint">
            family rollup
          </Badge>
        )}
        {cell.kind === "other" && (
          <Badge testid="harness-other" tone="faint">
            other — {cell.finished}
          </Badge>
        )}
        <span
          className={`ml-auto text-[15px] font-semibold ${cell.below_floor ? "text-faint" : ""}`}
          data-testid="harness-rate"
        >
          {pct}
        </span>
        <span className="font-mono text-[10.5px] text-faint">
          {cell.signed_off}/{cell.finished} signed off
        </span>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        {cell.below_floor && (
          <Badge testid="harness-below-floor" tone="faint">
            below the floor — {cell.finished} of {floor} attempts
          </Badge>
        )}
        {cell.skew && (
          <Badge testid="harness-skew" tone="warn" icon={<Scale size={11} />}>
            sampled by preference — {Math.round(cell.skew.share * 100)}%{" "}
            {cell.skew.reason.replace("_", " ")}
          </Badge>
        )}
        {!cell.is_current_version && (
          <Badge testid="harness-old-version" tone="faint">
            not the current version
          </Badge>
        )}
        {cell.versions_seen.length > 1 && (
          <Badge testid="harness-versions-seen" tone="faint">
            versions: {cell.versions_seen.join(", ")}
          </Badge>
        )}
      </div>

      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 font-mono text-[10.5px] text-faint">
        <span data-testid="harness-sampling">{samplingLabel(cell.sampling)}</span>
        {cell.median_seconds !== null && <span>median {cell.median_seconds}s</span>}
        <span data-testid="harness-cost">
          {costLabel(cell.build_cost ?? cell.cost)}
        </span>
      </div>
      {cell.samples && (
        <div className="mt-1.5 font-mono text-[10.5px] text-muted" data-testid="harness-samples">
          natural {cell.samples.natural.signed_off}/{cell.samples.natural.n}
          {cell.samples.natural.rate === null ? "" : ` (${Math.round(cell.samples.natural.rate * 100)}%)`}
          {" · "}
          probe {cell.samples.probe.signed_off}/{cell.samples.probe.n}
          {cell.samples.probe.rate === null ? "" : ` (${Math.round(cell.samples.probe.rate * 100)}%)`}
          {cell.samples.probe.below_floor ? " — probe below the floor" : ""}
        </div>
      )}
      {cell.utilization && (
        <div className="mt-1.5 font-mono text-[10.5px] text-faint" data-testid="harness-utilization">
          {costLabel(cell.utilization.tokens)}
          {" · "}
          {cell.utilization.turns.reason
            ? cell.utilization.turns.reason
            : `median ${cell.utilization.turns.median} turns / budget ${cell.utilization.turns.budget_median} (n ${cell.utilization.turns.reported})`}
          {" · "}
          {cell.utilization.budget_hits.reason
            ? cell.utilization.budget_hits.reason
            : `budget hit ${Math.round((cell.utilization.budget_hits.share ?? 0) * 100)}% (n ${cell.utilization.budget_hits.reported})`}
        </div>
      )}
      {(cell.build_cost || cell.review_cost) && (
        <div className="mt-1 font-mono text-[10.5px] text-faint" data-testid="harness-build-review-cost">
          cost of build {costLabel(cell.build_cost ?? cell.cost)}
          {" · "}
          cost of review {cell.review_cost ? costLabel(cell.review_cost) : "not reported"}
        </div>
      )}

      {cell.platform && (
        <div className="mt-2 font-mono text-[10.5px]" data-testid="harness-platform">
          {cell.platform.rate === null ? (
            <span className="text-faint">{cell.platform.reason}</span>
          ) : (
            <span className="text-muted">
              platform average {Math.round(cell.platform.rate * 100)}% across{" "}
              {cell.platform.orgs} organisations (n {cell.platform.n})
            </span>
          )}
        </div>
      )}

      {cell.by_project && cell.by_project.length > 0 && (
        <div className="mt-1.5 font-mono text-[10.5px] text-faint" data-testid="harness-by-project">
          {cell.by_project
            .map((p) => `${p.project_id} ${p.signed_off}/${p.finished}`)
            .join(" · ")}
        </div>
      )}

      {cell.leaves && cell.leaves.length > 0 && (
        <div className="mt-2 flex flex-col gap-1" data-testid="harness-leaves">
          {cell.leaves.map((leaf) => (
            <div
              key={cellId(leaf)}
              data-testid="harness-leaf"
              data-below-floor={leaf.below_floor ? "true" : "false"}
              className={`flex items-baseline gap-2 font-mono text-[11px] ${
                leaf.below_floor ? "text-faint" : "text-muted"
              }`}
            >
              <span>{leaf.label ?? leaf.key.capability}</span>
              <span>
                {leaf.rate === null ? "—" : `${Math.round(leaf.rate * 100)}%`}
              </span>
              <span>
                {leaf.signed_off}/{leaf.finished}
                {leaf.below_floor ? " — below the floor" : ""}
              </span>
            </div>
          ))}
        </div>
      )}

      <Series points={cell.series} />
    </div>
  );
}

function samplingLabel(s: HarnessSampling): string {
  const probe = s.probe ? ` · probe ${s.probe}` : "";
  return `first choice ${s.first_choice} · fallback ${s.fallback} · explicit ${s.explicit} · unknown ${s.unknown}${probe}`;
}

function costLabel(cost: HarnessCost): string {
  return cost.comparable
    ? `${cost.tokens_per_signed_off} tokens per signed-off item`
    : cost.reason;
}

function ReviewRow({ cell, floor }: { cell: HarnessReviewCell; floor: number }) {
  return (
    <div
      data-testid="harness-review-cell"
      className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3"
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-mono text-[12.5px]">
          {cell.key.vendor}
          {cell.key.model ? `:${cell.key.model}` : ""}
        </span>
        <span className="font-mono text-[10.5px] text-faint">
          {cell.key.capability} · {cell.key.size_band}
        </span>
        <Badge testid="harness-f2-label" tone="faint">
          {cell.f2.label}
        </Badge>
        <span className="ml-auto font-mono text-[10.5px] text-faint">
          {cell.checked} checked verdicts
        </span>
      </div>
      {cell.below_floor && (
        <div className="mt-2">
          <Badge testid="harness-review-below-floor" tone="faint">
            below the floor — {cell.checked} of {floor} checked verdicts
          </Badge>
        </div>
      )}
      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 font-mono text-[10.5px] text-muted">
        <span data-testid="harness-f1">
          F1 {cell.f1.rate === null ? "—" : `${Math.round(cell.f1.rate * 100)}%`} n={cell.f1.n}
        </span>
        <span data-testid="harness-f2">
          F2 miss {cell.f2.miss}/{cell.f2.n} unconfirmed {cell.f2.miss_unconfirmed}
        </span>
        <span data-testid="harness-f3">
          F3 unclassified {cell.f3.unclassified === null ? "—" : `${Math.round(cell.f3.unclassified * 100)}%`}
        </span>
      </div>
    </div>
  );
}

/**
 * The trend, drawn as one bar per recorded week.
 *
 * Weeks with no attempts are simply absent from the data and stay absent here: a gap is what
 * "nobody ran anything" looks like, and joining across it would draw a line through a week
 * nobody measured. Thin weeks are grey rather than dropped, for the same reason the totals
 * show them.
 */
function Series({ points }: { points: HarnessPoint[] }) {
  if (points.length === 0) return null;
  return (
    <div className="mt-2.5 flex items-end gap-1" data-testid="harness-series">
      {points.map((p) => (
        <div
          key={p.week}
          data-testid="harness-point"
          data-week={p.week}
          data-below-floor={p.below_floor ? "true" : "false"}
          title={`${p.week}: ${p.signed_off}/${p.finished} signed off${
            p.below_floor ? " — below the floor" : ""
          }`}
          className="flex-1"
        >
          <div
            className={`w-full rounded-[2px] ${p.below_floor ? "bg-line-2" : "bg-st-done"}`}
            style={{ height: `${4 + Math.round((p.rate ?? 0) * 26)}px` }}
          />
          <div className="mt-1 truncate text-center font-mono text-[9px] text-faint">
            {p.week.slice(-3)}
          </div>
        </div>
      ))}
    </div>
  );
}

type ProbeCandidate = { id: string; key: string; title: string; capabilities: string[]; touchpoints: string[] };

function ProbePanel({
  projectId,
  suggestions,
  labels,
}: {
  projectId: string;
  suggestions: { vendor: string; model: string; binary_version: string; trigger: string; reason: string }[];
  labels?: Record<string, string>;
}) {
  const { data, isLoading } = useHarnessProbeCandidates(projectId);
  const startRun = useStartHarnessProbeRun(projectId);

  const [pickedSuggestion, setPickedSuggestion] = useState<number | null>(null);
  const [pickedItems, setPickedItems] = useState<Record<string, string[]>>({});
  const [error, setError] = useState<string | null>(null);

  if (isLoading) return null;
  if (!data) return null;

  const hasCandidates = Object.keys(data.by_leaf).length > 0;
  if (!hasCandidates && suggestions.length === 0) return null;

  const suggestion = pickedSuggestion !== null ? suggestions[pickedSuggestion] : null;

  const fallbackLeaves = new Set<string>();
  for (const [fam, group] of Object.entries(data.by_family)) {
    if (group.fallback) {
      for (const leaf of group.leaf_ready) fallbackLeaves.add(leaf);
      void fam;
    }
  }

  const groups: { key: string; label: string; items: ProbeCandidate[] }[] = [];
  for (const [fam, group] of Object.entries(data.by_family)) {
    if (group.fallback && group.items.length > 0) {
      groups.push({ key: fam, label: labels?.[fam] ?? `${fam} (family)`, items: group.items });
    }
  }
  for (const [leaf, items] of Object.entries(data.by_leaf)) {
    if (fallbackLeaves.has(leaf)) continue;
    if (items.length > 0) {
      groups.push({ key: leaf, label: labels?.[leaf] ?? leaf, items });
    }
  }

  const totalPicked = Object.values(pickedItems).reduce((s, a) => s + a.length, 0);

  const toggleItem = (groupKey: string, itemId: string) => {
    setPickedItems((prev) => {
      const cur = prev[groupKey] ?? [];
      if (cur.includes(itemId)) return { ...prev, [groupKey]: cur.filter((id) => id !== itemId) };
      if (cur.length >= 3) return prev;
      return { ...prev, [groupKey]: [...cur, itemId] };
    });
  };

  const handleStart = () => {
    if (!suggestion) return;
    setError(null);
    for (const [cap, ids] of Object.entries(pickedItems)) {
      if (ids.length === 0) continue;
      startRun.mutate(
        {
          vendor: suggestion.vendor,
          model: suggestion.model,
          capability: cap,
          item_ids: ids,
          trigger: suggestion.trigger,
          binary_version: suggestion.binary_version,
        },
        {
          onSuccess: () => {
            setPickedItems((prev) => {
              const next = { ...prev };
              delete next[cap];
              return next;
            });
          },
          onError: (err: Error) => {
            const msg = err.message || String(err);
            if (msg.includes("409") || msg.includes("already running")) {
              setError(`A probe for ${suggestion.vendor}:${suggestion.model} is already running — one model and one leaf at a time.`);
            } else {
              setError(msg);
            }
          },
        },
      );
      break;
    }
  };

  const estimateLabel = data.estimated_tokens.comparable
    ? `~${Math.round(data.estimated_tokens.tokens_per_attempt ?? 0).toLocaleString()} tokens per attempt (${data.estimated_tokens.reported} reported)`
    : data.estimated_tokens.reason ?? "no estimate";

  return (
    <div data-testid="harness-probe-panel" className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-[13px] font-semibold">Probe</span>
        <span className="font-mono text-[10.5px] text-faint" data-testid="harness-probe-estimate">
          {estimateLabel}
        </span>
      </div>

      {suggestions.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5" data-testid="harness-probe-suggestions">
          {suggestions.map((s, i) => {
            const active = pickedSuggestion === i;
            return (
              <button
                key={`${s.vendor}:${s.model}:${s.binary_version}`}
                type="button"
                data-testid="harness-probe-suggestion"
                onClick={() => { setPickedSuggestion(active ? null : i); setError(null); }}
                className={`rounded-[6px] border px-2 py-1 font-mono text-[11px] transition-colors ${
                  active
                    ? "border-st-done bg-[rgba(80,200,120,0.12)] text-st-done"
                    : "border-line-2 bg-surface text-muted hover:border-line"
                }`}
              >
                {s.vendor}:{s.model}
                {s.binary_version ? `:${s.binary_version}` : ""}
                <span className="ml-1 text-faint">({s.trigger})</span>
              </button>
            );
          })}
        </div>
      )}

      {hasCandidates && (
        <div className="mt-3 flex flex-col gap-2.5" data-testid="harness-probe-candidates">
          {groups.map((group) => {
            const selected = pickedItems[group.key] ?? [];
            return (
              <div key={group.key} data-testid="harness-probe-group">
                <div className="flex items-baseline gap-2 font-mono text-[11px]">
                  <span className="text-muted">{group.label}</span>
                  <span className="text-faint">{group.items.length} candidates</span>
                  {selected.length > 0 && (
                    <span className="text-st-done">{selected.length} picked</span>
                  )}
                </div>
                <div className="mt-1 flex flex-col gap-0.5">
                  {group.items.map((item) => {
                    const checked = selected.includes(item.id);
                    return (
                      <label
                        key={item.id}
                        data-testid="harness-probe-item"
                        className={`flex cursor-pointer items-start gap-2 rounded-[4px] px-2 py-1 font-mono text-[11px] transition-colors ${
                          checked ? "bg-[rgba(80,200,120,0.06)]" : "hover:bg-surface"
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={!checked && totalPicked >= 3}
                          onChange={() => toggleItem(group.key, item.id)}
                          className="mt-0.5 accent-st-done"
                        />
                        <span className="text-muted">{item.title}</span>
                      </label>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {error && (
        <div data-testid="harness-probe-error" className="mt-2 rounded-[6px] border border-[rgba(240,100,100,0.3)] bg-[rgba(240,100,100,0.08)] px-2.5 py-1.5 font-mono text-[11px] text-[#f06464]">
          {error}
        </div>
      )}

      {suggestion && totalPicked > 0 && (
        <div className="mt-3 flex items-center gap-2">
          <button
            type="button"
            data-testid="harness-probe-start"
            disabled={startRun.isPending}
            onClick={handleStart}
            className="inline-flex items-center gap-1.5 rounded-[6px] border border-st-done bg-[rgba(80,200,120,0.12)] px-3 py-1.5 font-mono text-[11.5px] text-st-done transition-colors hover:bg-[rgba(80,200,120,0.2)] disabled:opacity-50"
          >
            <Play size={12} />
            {startRun.isPending ? "Starting…" : `Start probe — ${suggestion.vendor}:${suggestion.model}`}
          </button>
          <span className="font-mono text-[10.5px] text-faint">
            {totalPicked} item{totalPicked === 1 ? "" : "s"} → {suggestion.trigger}
          </span>
        </div>
      )}
    </div>
  );
}

function Badge({
  children,
  testid,
  tone,
  icon,
}: {
  children: React.ReactNode;
  testid: string;
  tone: "faint" | "warn";
  icon?: React.ReactNode;
}) {
  const cls =
    tone === "warn"
      ? "border-[rgba(240,180,80,0.35)] bg-[rgba(240,180,80,0.1)] text-[#f0b450]"
      : "border-line-2 bg-surface text-faint";
  return (
    <span
      data-testid={testid}
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10.5px] ${cls}`}
    >
      {icon ?? (tone === "warn" ? <AlertTriangle size={11} /> : null)}
      {children}
    </span>
  );
}
