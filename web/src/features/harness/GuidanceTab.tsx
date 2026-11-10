import { useState } from "react";
import { Check, Copy, Signpost } from "lucide-react";

import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/cn";
import type { HarnessGuidance, HarnessRoutingRow } from "@/lib/types";

const MATRIX_BANDS = ["S", "M", "L"] as const;

/** Verdict pill tones. A server that predates grading sends measured / below_floor: neutral. */
const VERDICT_TONE: Record<string, string> = {
  PREFER: "border-[rgba(95,208,122,0.35)] bg-[rgba(95,208,122,0.1)] text-st-done",
  OK: "border-line-2 bg-surface-3/50 text-muted",
  AVOID: "border-[rgba(255,107,107,0.35)] bg-[rgba(255,107,107,0.08)] text-st-blocked",
  PROBE: "border-purple/35 bg-purple/10 text-purple",
};
const NEUTRAL = "border-line-2 text-faint";

function VerdictPill({ verdict, className }: { verdict: string; className?: string }) {
  return (
    <span
      data-testid="guidance-cell-verdict"
      className={cn("rounded-[4px] border px-[5px] py-px font-mono text-[9px] tracking-[0.5px]", VERDICT_TONE[verdict] ?? NEUTRAL, className)}
    >
      {verdict}
    </span>
  );
}

function GuidanceMatrixCell({ row, accent }: { row: HarnessRoutingRow | undefined; accent: (pick: string) => string }) {
  if (!row) {
    return (
      <div
        role="cell"
        className="border-l border-line-2/60 px-3 py-2.5 font-mono text-[10px] text-faint"
        data-testid="guidance-matrix-cell"
        data-empty="true"
      >
        not served — no attempts
      </div>
    );
  }
  const fallback = row.fallback
    ? `then ${row.fallback}${row.fallback_rate == null ? "" : ` ${Math.round(row.fallback_rate * 100)}%`}`
    : row.verdict === "PROBE" ? "no measured fallback — a guess" : "no measured fallback";
  return (
    <div
      role="cell"
      className="flex min-w-0 flex-col gap-[5px] border-l border-line-2/60 px-3 py-2.5"
      data-testid="guidance-matrix-cell"
      data-function={row.function}
      data-band={row.effort_band}
    >
      <div className="flex items-center gap-1.5">
        <VerdictPill verdict={row.verdict} />
        <span className="flex-1" />
        {row.confidence ? (
          <span data-testid="guidance-cell-confidence" className="font-mono text-meta text-faint">{row.confidence}</span>
        ) : (
          <span data-testid="guidance-confidence-absent" className="font-mono text-meta text-faint">confidence not served</span>
        )}
      </div>
      <div className="flex min-w-0 items-center gap-1.5">
        <span className="size-1.5 flex-none rounded-[2px]" style={{ background: accent(row.pick) }} />
        <span className="truncate font-mono text-[11.5px] text-fg" title={row.pick}>{row.pick}</span>
      </div>
      <div className="font-mono text-[10px] text-muted">{row.evidence}</div>
      <div className="font-mono text-[10px] text-faint">{fallback}</div>
    </div>
  );
}

function formatStamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

/**
 * PRD-47 S12 — what the supervisor is served: the routing table per function and band, graded
 * on the server (PREFER / OK / AVOID / PROBE), the rules that grade it, and the exact routing
 * text `fleet_status` carries. Nothing here grades or re-ranks; it shows the payload.
 */
export function GuidanceTab({
  guidance,
  order = [],
  accent,
}: {
  guidance?: HarnessGuidance;
  /** Catalog leaf order, so rows read A1…F3 rather than alphabetically. */
  order?: string[];
  accent: (pick: string) => string;
}) {
  const routingRows = guidance?.routing ?? [];
  const stamp = guidance?.generation_stamp;
  const verdictRules = guidance?.verdict_rules ?? [];
  const served = guidance?.served_text || guidance?.fleet_status_text || "no fleet_status served";
  const [copied, setCopied] = useState(false);

  const byCell = new Map(routingRows.map((r) => [`${r.function}\u0000${r.effort_band}`, r]));
  const labels = new Map(routingRows.map((r) => [r.function, r.label]));
  const rank = (f: string) => {
    const i = order.indexOf(f);
    return i < 0 ? order.length : i;
  };
  const functions = [...new Set(routingRows.map((r) => r.function))].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));

  const handleCopy = async () => {
    if (await copyText(served)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    }
  };

  const cols = "170px repeat(3,minmax(0,1fr))";

  return (
    <div className="flex max-w-[1180px] flex-col gap-4" data-testid="guidance-matrix">
      <div className="flex flex-wrap items-start gap-3.5 rounded-[14px] border border-purple/20 bg-[linear-gradient(160deg,rgba(167,139,250,0.06),var(--color-bg))] px-[18px] py-4">
        <span className="flex size-[30px] flex-none items-center justify-center rounded-[9px] bg-purple text-bg">
          <Signpost size={15} />
        </span>
        <div className="min-w-0 flex-[1_1_420px]">
          <div className="text-lead font-semibold text-purple-2">Routing guidance served to gbfleet</div>
          <p className="mt-1 max-w-[96ch] text-body leading-[1.6] text-muted">
            When <code className="font-mono text-[11.5px] text-fg-2">spawn(tier=)</code> arrives with a
            function and an effort band, the supervisor reads this table before your weights and
            policy. It is graded on the server from the measurements on the Performance tab — it
            never cites a pick below the floor, and it says PROBE when it is guessing.
          </p>
        </div>
        {stamp && (
          <div data-testid="guidance-generation-stamp" className="flex flex-col items-end gap-1 font-mono text-[10px] text-faint">
            <span>generated {formatStamp(stamp.generated_at)}</span>
            <span>{stamp.window_days}d window · floor {stamp.floor} · {stamp.attempts} attempts</span>
            <span className={stamp.supervisors_served > 0 ? "text-st-done" : undefined}>
              {stamp.supervisors_served > 0
                ? `served to ${stamp.supervisors_served} supervisor${stamp.supervisors_served === 1 ? "" : "s"}`
                : "no supervisor online"}
            </span>
          </div>
        )}
      </div>

      {routingRows.length === 0 ? (
        <div className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-2.5 text-body text-muted">
          No measured routing served yet. A row appears once a delegation finishes in this window.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-[12px] border border-line-2 bg-surface">
          <div role="table" aria-label="Routing guidance" className="min-w-[760px]">
            <div role="row" className="grid border-b border-line-2" style={{ gridTemplateColumns: cols }}>
              <div role="columnheader" className="px-3.5 py-2.5 font-mono text-meta tracking-[0.6px] text-faint">FUNCTION ↓ · EFFORT →</div>
              {MATRIX_BANDS.map((b) => (
                <div key={b} role="columnheader" className="border-l border-line-2/60 px-3 py-2.5 text-[12px] font-semibold text-fg-2">
                  {b}
                </div>
              ))}
            </div>
            {functions.map((fn) => (
              <div key={fn} role="row" data-testid="guidance-matrix-row" data-function={fn} className="grid border-b border-line-2/60 last:border-b-0" style={{ gridTemplateColumns: cols }}>
                <div role="rowheader" className="px-3.5 py-3">
                  <div className="text-body font-medium text-fg">{labels.get(fn) ?? fn}</div>
                  {labels.get(fn) && labels.get(fn) !== fn && <div className="font-mono text-meta text-faint">{fn}</div>}
                </div>
                {MATRIX_BANDS.map((band) => (
                  <GuidanceMatrixCell key={band} row={byCell.get(`${fn}\u0000${band}`)} accent={accent} />
                ))}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="grid grid-cols-[repeat(auto-fit,minmax(340px,1fr))] items-start gap-4">
        <section data-testid="guidance-grading-rules" className="rounded-[12px] border border-line-2 bg-surface px-4 py-3.5">
          <h2 className="mb-1 text-lead font-semibold">Grading rules</h2>
          <p className="mb-3 text-[12px] text-muted">How a routing cell becomes a verdict. Decided on the server; this page does not grade.</p>
          {verdictRules.length === 0 ? (
            <p className="text-[12px] text-faint">This server does not send its verdict rules.</p>
          ) : (
            <div className="flex flex-col gap-2">
              {verdictRules.map((r) => (
                <div key={r.id} data-testid="guidance-rule" data-rule={r.id} className="flex items-start gap-2.5">
                  <VerdictPill verdict={r.id} className="min-w-[72px] flex-none py-0.5 text-center" />
                  <span className="text-[12px] leading-[1.55] text-fg-2">{r.detail}</span>
                </div>
              ))}
            </div>
          )}
        </section>
        <section data-testid="guidance-as-served" className="flex flex-col gap-2.5 rounded-[12px] border border-line-2 bg-surface px-4 py-3.5">
          <div className="flex items-center gap-2">
            <h2 className="flex-1 text-lead font-semibold">As served</h2>
            <button
              type="button"
              data-testid="guidance-fleet-status-copy"
              onClick={() => void handleCopy()}
              className={cn("inline-flex h-[26px] items-center gap-1 rounded-[7px] border border-control bg-surface-2 px-2.5 text-small hover:text-fg-2", copied ? "text-st-done" : "text-muted")}
            >
              {copied ? <Check size={12} /> : <Copy size={12} />}
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <p className="text-[12px] text-muted">
            The exact text a supervisor receives from <code className="font-mono text-small text-fg-2">fleet_status</code>. Nothing here is hidden from the agents.
          </p>
          <pre
            data-testid="guidance-fleet-status"
            className="max-h-[340px] overflow-auto whitespace-pre-wrap rounded-[9px] border border-line-2 bg-bg px-3.5 py-3 font-mono text-small leading-[1.65] text-muted-2"
          >
            {served}
          </pre>
        </section>
      </div>
    </div>
  );
}
