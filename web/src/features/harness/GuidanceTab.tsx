import type { HarnessGuidance } from "@/lib/types";

function formatThresholds(thresholds: Record<string, number>): string {
  return Object.entries(thresholds)
    .map(([k, v]) => `${k}: ${v}`)
    .join(", ");
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

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      <section data-testid="guidance-routing-table">
        <h3 className="mb-2 text-[13px] font-semibold">Routing table</h3>
        <p className="mb-3 text-[12px] text-muted">
          Per function and effort band — ranked here from the measured layers in{" "}
          <span className="font-mono">fleet_status.measured</span>, not read from what the
          supervisor resolved. The pick, evidence and fallback are derived from project-layer
          cells; a column with no server field reads as not measured rather than being dropped.
        </p>
        {routingRows.length === 0 ? (
          <div className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-[12.5px] text-muted">
            No measured routing served yet. A row appears once a delegation finishes in this
            window.
          </div>
        ) : (
          <div className="overflow-x-auto rounded-[10px] border border-line-2">
            <table className="w-full text-left font-mono text-[11.5px]">
              <thead>
                <tr className="border-b border-line-2 bg-surface-2">
                  <th className="px-3 py-2 text-faint">function</th>
                  <th className="px-3 py-2 text-faint">effort band</th>
                  <th className="px-3 py-2 text-faint">verdict</th>
                  <th className="px-3 py-2 text-faint">attempts (n)</th>
                  <th className="px-3 py-2 text-faint">pick</th>
                  <th className="px-3 py-2 text-faint">evidence</th>
                  <th className="px-3 py-2 text-faint">fallback</th>
                </tr>
              </thead>
              <tbody>
                {routingRows.map((row, i) => (
                  <tr
                    key={`${row.function}:${row.effort_band}`}
                    data-testid="guidance-routing-row"
                    className={i % 2 === 0 ? "bg-surface" : "bg-surface-2"}
                  >
                    <td className="px-3 py-1.5 text-muted">{row.function}</td>
                    <td className="px-3 py-1.5 text-faint">{row.effort_band}</td>
                    <td className="px-3 py-1.5">
                      <span
                        data-testid="guidance-routing-verdict"
                        className={row.verdict === "measured" ? "text-st-done" : "text-faint"}
                      >
                        {row.verdict}
                      </span>
                    </td>
                    <td className="px-3 py-1.5 text-faint">{row.attempts}</td>
                    <td className="px-3 py-1.5 text-muted">{row.pick}</td>
                    <td className="px-3 py-1.5 text-faint">{row.evidence}</td>
                    <td className="px-3 py-1.5 text-faint">{row.fallback ?? "—"}</td>
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
                <span className="text-[12.5px] font-medium">{rule.title}</span>
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

      {stamp && (
        <section data-testid="guidance-generation-stamp">
          <h3 className="mb-1 text-[13px] font-semibold">Generation stamp</h3>
          <p className="font-mono text-[11.5px] text-faint">
            window {stamp.window_days}d · floor {stamp.floor} · attempts {stamp.attempts} ·
            supervisors served {stamp.supervisors_served}
          </p>
        </section>
      )}

      <section data-testid="guidance-as-served">
        <h3 className="mb-2 text-[13px] font-semibold">As served</h3>
        <p className="mb-3 text-[12px] text-muted">
          The verbatim <span className="font-mono">fleet_status</span> text the supervisor
          received. This is the raw input — nothing on this page interprets it.
        </p>
        <pre
          data-testid="guidance-fleet-status"
          className="max-h-80 overflow-auto rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 font-mono text-[11px] text-muted"
        >
          {fleetStatusText}
        </pre>
      </section>
    </div>
  );
}
