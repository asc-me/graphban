import { useState } from "react";
import { Check, Copy } from "lucide-react";

import { copyText } from "@/lib/clipboard";
import type { HarnessGuidance, HarnessRoutingRow } from "@/lib/types";

const MATRIX_BANDS = ["S", "M", "L"] as const;

function formatThresholds(thresholds: Record<string, number>): string {
  return Object.entries(thresholds)
    .map(([k, v]) => `${k}: ${v}`)
    .join(", ");
}

function matrixIndex(routing: HarnessRoutingRow[]): Map<string, HarnessRoutingRow> {
  const m = new Map<string, HarnessRoutingRow>();
  for (const row of routing) {
    m.set(`${row.function}\u0000${row.effort_band}`, row);
  }
  return m;
}

function GuidanceMatrixCell({ row }: { row: HarnessRoutingRow | undefined }) {
  if (!row) {
    return (
      <td
        className="border border-line-2 px-2 py-2 align-top text-faint"
        data-testid="guidance-matrix-cell"
        data-band={undefined}
        data-empty="true"
      >
        <span className="font-mono text-[10px]">not served</span>
      </td>
    );
  }

  const hasConfidence = Object.prototype.hasOwnProperty.call(row, "confidence");

  return (
    <td
      className="border border-line-2 px-2 py-2 align-top"
      data-testid="guidance-matrix-cell"
      data-function={row.function}
      data-band={row.effort_band}
    >
      <div
        data-testid="guidance-cell-verdict"
        className={`font-mono text-small font-semibold ${row.verdict === "measured" ? "text-st-done" : "text-faint"}`}
      >
        {row.verdict}
      </div>
      <dl className="mt-1 flex flex-col gap-0.5 font-mono text-[10px] text-muted">
        <div>
          <span className="text-faint">pick </span>
          {row.pick}
        </div>
        <div>
          <span className="text-faint">evidence </span>
          {row.evidence}
        </div>
        <div>
          <span className="text-faint">fallback </span>
          {row.fallback ?? "—"}
        </div>
        <div>
          <span className="text-faint">n </span>
          {row.attempts}
        </div>
      </dl>
      {hasConfidence ? (
        <div data-testid="guidance-cell-confidence" className="mt-1 font-mono text-[10px] text-faint">
          confidence {String(row.confidence)}
        </div>
      ) : (
        <div data-testid="guidance-confidence-absent" className="mt-1 font-mono text-[10px] text-faint">
          confidence not served
        </div>
      )}
    </td>
  );
}

/**
 * PRD-47 S12 — what the supervisor was actually served: routing per function and band,
 * the grading rules that produce recommendations, and the raw fleet_status text.
 */
export function GuidanceTab({ guidance }: { guidance?: HarnessGuidance }) {
  const rules = guidance?.grading_rules ?? [];
  const routingRows = guidance?.routing ?? [];
  const stamp = guidance?.generation_stamp;
  const fleetStatusText = guidance?.fleet_status_text ?? "no fleet_status served";
  const [copied, setCopied] = useState(false);

  const byCell = matrixIndex(routingRows);
  const functions = [...new Set(routingRows.map((r) => r.function))].sort();

  const handleCopy = async () => {
    const ok = await copyText(fleetStatusText);
    if (ok) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4">
      <section data-testid="guidance-matrix">
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="text-[13px] font-semibold">Guidance for gbfleet</h3>
          {stamp && (
            <p
              data-testid="guidance-generation-stamp"
              className="font-mono text-small text-faint"
            >
              window {stamp.window_days}d · floor {stamp.floor} · attempts {stamp.attempts} ·
              supervisors served {stamp.supervisors_served}
            </p>
          )}
        </div>
        <p className="mb-3 text-[12px] text-muted">
          One row per function, columns S, M, and L — each cell is the routing row{" "}
          <span className="font-mono">GET /api/harness/guidance</span> already returns. The verdict
          text is the payload&apos;s <span className="font-mono">verdict</span>, not a substituted
          confidence word.
        </p>
        {routingRows.length === 0 ? (
          <div className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-body text-muted">
            No measured routing served yet. A row appears once a delegation finishes in this
            window.
          </div>
        ) : (
          <div className="overflow-x-auto rounded-[10px] border border-line-2">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="border-b border-line-2 bg-surface-2">
                  <th className="px-3 py-2 font-mono text-[10px] text-faint">function</th>
                  {MATRIX_BANDS.map((band) => (
                    <th key={band} className="border border-line-2 px-2 py-2 font-mono text-[10px] text-faint">
                      {band}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {functions.map((fn) => (
                  <tr key={fn} data-testid="guidance-matrix-row" data-function={fn}>
                    <td className="border border-line-2 bg-surface-2 px-3 py-2 font-mono text-small text-muted">
                      {fn}
                    </td>
                    {MATRIX_BANDS.map((band) => (
                      <GuidanceMatrixCell key={band} row={byCell.get(`${fn}\u0000${band}`)} />
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section data-testid="guidance-grading-rules">
        <h3 className="mb-2 text-[13px] font-semibold">Grading rules</h3>
        <p className="mb-3 text-[12px] text-muted">
          R1–R6 produce the recommendation cards on the Changes tab. Each fires on cells
          above the floor; nothing fires on a cell that has not earned its count.
        </p>
        <div className="flex flex-col gap-2">
          {rules.map((rule) => (
            <div
              key={rule.id}
              data-testid="guidance-rule"
              data-rule={rule.id}
              className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5"
            >
              <div className="flex items-baseline gap-2">
                <span className="rounded-full border border-line-2 px-1.5 py-0.5 font-mono text-[10px] text-faint">
                  {rule.id}
                </span>
                <span className="text-body font-medium">{rule.title}</span>
              </div>
              <p className="mt-1 text-[12px] text-muted">{rule.detail}</p>
              <div
                data-testid="guidance-rule-thresholds"
                className="mt-1 font-mono text-[10.5px] text-faint"
              >
                {formatThresholds(rule.thresholds)}
              </div>
            </div>
          ))}
        </div>
      </section>

      <section data-testid="guidance-as-served">
        <div className="mb-2 flex items-center justify-between gap-2">
          <h3 className="text-[13px] font-semibold">As served</h3>
          <button
            type="button"
            data-testid="guidance-fleet-status-copy"
            onClick={() => void handleCopy()}
            className="inline-flex items-center gap-1 rounded-[6px] border border-control bg-surface-2 px-2 py-1 font-mono text-small text-muted hover:text-fg-2"
          >
            {copied ? <Check size={12} className="text-accent" /> : <Copy size={12} />}
            Copy
          </button>
        </div>
        <p className="mb-3 text-[12px] text-muted">
          The verbatim <span className="font-mono">fleet_status</span> text the supervisor
          received. This is the raw input — nothing on this page interprets it.
        </p>
        <pre
          data-testid="guidance-fleet-status"
          className="max-h-80 overflow-auto rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 font-mono text-small text-muted"
        >
          {fleetStatusText}
        </pre>
      </section>
    </div>
  );
}
