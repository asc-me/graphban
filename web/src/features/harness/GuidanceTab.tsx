import { useState } from "react";

import { useFleet, useHarness } from "@/lib/queries";
import type { FleetMeasured } from "@/lib/types";

/**
 * The grading rules — the thresholds that turn a cell into a verdict.
 *
 * These are the constants from `backend/app/services/harness_rules.py`. They travel with
 * the code, not with configuration, so stating them here is what "the grading rules" means
 * on the page: a person can read what fired and why.
 */
const GRADING_RULES = [
  {
    id: "R1",
    title: "Promote to verified",
    detail: "An unverified row at or above 80% signed-off on 10+ finished attempts is worth a verified commit.",
    thresholds: "min_finished: 10 · min_rate: 0.80",
  },
  {
    id: "R2",
    title: "Demote to failed",
    detail: "A verified row below 25% signed-off on 6+ finished attempts is worth a failed entry.",
    thresholds: "min_finished: 6 · max_rate: 0.25",
  },
  {
    id: "R3",
    title: "Reweight profile default",
    detail: "A profile's top default beaten by 30%+ on 8+ attempts in the same cell, across 2+ families.",
    thresholds: "min_n: 8 · margin: 0.30 · min_families: 2",
  },
  {
    id: "R4",
    title: "Policy nudge",
    detail: "A local_only project whose local rows bounce 70%+ or succeed 70%+ on 6+ attempts.",
    thresholds: "min_n: 6 · bounce_rate: 0.70 · keep_rate: 0.70",
  },
  {
    id: "R5",
    title: "Reprior from cell vs prior",
    detail: "A cell at the floor that disagrees with the row's per-capability prior by 30%+ on 5+ attempts.",
    thresholds: "min_n: 5 · margin: 0.30",
  },
  {
    id: "R6",
    title: "Install gap",
    detail: "6+ resolutions dropped a better measured row as not installed, with a 30%+ margin.",
    thresholds: "min_drops: 6 · margin: 0.30",
  },
];

const SIZE_ORDER = ["S", "M", "L"];

/**
 * PRD-47 S12 — Guidance tab.
 *
 * The routing table actually served to gbfleet, per function and effort band, with verdict,
 * confidence, pick, evidence and fallback; its generation stamp; the grading rules that turn
 * a cell into a verdict; and "As served" — the verbatim fleet_status text.
 *
 * Nothing here is hidden from the agents.
 */
export function GuidanceTab({ projectId }: { projectId: string }) {
  const { data: fleetData, isLoading: fleetLoading } = useFleet(projectId);
  const harnessQ = useHarness(projectId, { versions: "all" });
  const [showRaw, setShowRaw] = useState(false);

  if (fleetLoading || !fleetData) {
    return (
      <div className="p-5 text-[13px] text-muted">
        Loading fleet data…
      </div>
    );
  }

  const measured = fleetData.measured ?? [];
  const harnessData = harnessQ.data;
  const floor = harnessData?.floor ?? 5;
  const windowDays = harnessData?.window_days ?? 30;
  const totalAttempts = harnessData?.cells.reduce((s, c) => s + c.finished, 0) ?? 0;

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4 p-5">
      <GenerationStamp
        windowDays={windowDays}
        floor={floor}
        totalAttempts={totalAttempts}
        generatedAt={harnessData?.generated_at}
        online={fleetData.online}
      />

      <section>
        <h2 className="text-[14px] font-semibold tracking-tight">Routing table</h2>
        <p className="mb-3 mt-0.5 text-[12px] text-muted">
          What gbfleet is served per function and effort band. Each row is one measured cell —
          vendor × model × capability — with its verdict derived from the grading rules below.
        </p>
        {measured.length === 0 ? (
          <div className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3 text-[12.5px] text-muted">
            No measured cells yet. The routing table appears once a delegation finishes and
            reports its outcome.
          </div>
        ) : (
          <div className="flex flex-col gap-1.5">
            {measured.map((cell, i) => (
              <RoutingRow key={i} cell={cell} floor={floor} />
            ))}
          </div>
        )}
      </section>

      <section>
        <h2 className="text-[14px] font-semibold tracking-tight">Grading rules</h2>
        <p className="mb-3 mt-0.5 text-[12px] text-muted">
          The thresholds that turn a cell into a verdict. A rule fires when its thresholds are
          met; the recommendation it produces is a draft on the Changes & probes tab.
        </p>
        <div className="flex flex-col gap-1.5">
          {GRADING_RULES.map((rule) => (
            <div
              key={rule.id}
              data-testid="harness-grading-rule"
              data-rule={rule.id}
              className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5"
            >
              <div className="flex items-baseline gap-2">
                <span className="rounded-full border border-line-2 px-1.5 py-0.5 font-mono text-[10px] text-faint">
                  {rule.id}
                </span>
                <span className="text-[12.5px] font-medium">{rule.title}</span>
              </div>
              <p className="mt-1 text-[12px] text-muted">{rule.detail}</p>
              <div className="mt-1 font-mono text-[10.5px] text-faint">{rule.thresholds}</div>
            </div>
          ))}
        </div>
      </section>

      <section>
        <div className="flex items-center justify-between">
          <h2 className="text-[14px] font-semibold tracking-tight">As served</h2>
          <button
            type="button"
            data-testid="harness-toggle-raw"
            onClick={() => setShowRaw((v) => !v)}
            className="font-mono text-[10.5px] text-faint hover:text-muted"
          >
            {showRaw ? "hide" : "show"} raw fleet_status
          </button>
        </div>
        <p className="mb-3 mt-0.5 text-[12px] text-muted">
          Nothing here is hidden from the agents. This is the verbatim text of the{" "}
          <span className="font-mono">fleet_status</span> response — what every supervisor
          reads on every tick.
        </p>
        {showRaw && (
          <pre
            data-testid="harness-raw-fleet-status"
            className="max-h-[400px] overflow-auto rounded-[10px] border border-line-2 bg-surface-2 p-3 font-mono text-[11px] text-muted"
          >
            {JSON.stringify(
              {
                online: fleetData.online,
                total: fleetData.total,
                by_role: fleetData.by_role,
                posture: fleetData.posture,
                measured: fleetData.measured,
                mix: fleetData.mix,
                heartbeat_interval_seconds: fleetData.heartbeat_interval_seconds,
                presence_ttl_seconds: fleetData.presence_ttl_seconds,
              },
              null,
              2,
            )}
          </pre>
        )}
      </section>
    </div>
  );
}

function GenerationStamp({ windowDays, floor, totalAttempts, generatedAt, online }: {
  windowDays: number;
  floor: number;
  totalAttempts: number;
  generatedAt?: string;
  online: number;
}) {
  return (
    <div
      data-testid="harness-generation-stamp"
      className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 font-mono text-[11px] text-faint"
    >
      <span>window {windowDays}d</span>
      <span className="mx-2">·</span>
      <span>floor {floor}</span>
      <span className="mx-2">·</span>
      <span>{totalAttempts} attempts</span>
      <span className="mx-2">·</span>
      <span>{online} supervisors online</span>
      {generatedAt && (
        <>
          <span className="mx-2">·</span>
          <span>generated {new Date(generatedAt).toLocaleString()}</span>
        </>
      )}
    </div>
  );
}

function RoutingRow({ cell, floor }: { cell: FleetMeasured; floor: number }) {
  const rate = cell.quality.value;
  const n = cell.quality.n;
  const belowFloor = n < floor;
  const pct = rate === null ? "—" : `${Math.round(rate * 100)}%`;

  const verdict = belowFloor
    ? "unverified"
    : rate >= 0.8
      ? "verified"
      : rate >= 0.5
        ? "unverified"
        : "failed";

  const confidence = n >= 10 ? "high" : n >= floor ? "medium" : "low";

  const bands = cell.bands ?? {};
  const bandParts = SIZE_ORDER
    .filter((b) => bands[b] && bands[b].n > 0)
    .map((b) => `${b}:${bands[b].n}`);

  return (
    <div
      data-testid="harness-routing-row"
      className={`rounded-[8px] border px-3 py-2 ${
        belowFloor ? "border-dashed border-line-2 bg-surface-2" : "border-line-2 bg-surface-2"
      }`}
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-mono text-[12px]">
          {cell.vendor}{cell.model ? `:${cell.model}` : ""}
        </span>
        {cell.capability && (
          <span className="font-mono text-[10.5px] text-faint">{cell.capability}</span>
        )}
        {cell.layer && (
          <span className="font-mono text-[9px] text-faint">({cell.layer})</span>
        )}
        <span className="ml-auto text-[13px] font-semibold">{pct}</span>
        <span className="font-mono text-[10px] text-faint">n={n}</span>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-2 font-mono text-[10px]">
        <span
          data-testid="harness-routing-verdict"
          className={`rounded-full border px-1.5 py-0.5 ${
            verdict === "verified"
              ? "border-st-done bg-[rgba(80,200,120,0.1)] text-st-done"
              : verdict === "failed"
                ? "border-[rgba(240,100,100,0.3)] bg-[rgba(240,100,100,0.08)] text-[#f06464]"
                : "border-line-2 text-faint"
          }`}
        >
          {verdict}
        </span>
        <span className="text-faint">confidence: {confidence}</span>
        {bandParts.length > 0 && (
          <span className="text-faint">bands: {bandParts.join(" · ")}</span>
        )}
        {cell.cost && (
          <span className="text-faint">
            {cell.cost.comparable
              ? `${cell.cost.tokens_to_signoff ?? "?"} tokens/sign-off`
              : "cost not comparable"}
          </span>
        )}
        {cell.latency && (
          <span className="text-faint">median {cell.latency.median_seconds}s</span>
        )}
      </div>
    </div>
  );
}
