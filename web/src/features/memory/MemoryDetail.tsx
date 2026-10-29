import type { ReactNode } from "react";

import { cn } from "@/lib/cn";
import type { CandidateJudge } from "@/lib/types";

import { QUEUE_BY_ID, confBand, judgeWords, type Row } from "./queues";

export type Tone = "lime" | "neutral" | "purple" | "red";

/** One decision on a row: a button in the detail panel, a key, and a bulk action. */
export interface Decision {
  id: string;
  label: string;
  sub: string;
  key: string;
  tone: Tone;
  /** Why it cannot run here. A decision with no endpoint behind it says so, not nothing. */
  disabled?: string;
}

export const TONE: Record<Tone, string> = {
  lime: "border-accent/45 bg-accent/10 text-accent",
  neutral: "border-line-2 text-fg-2",
  purple: "border-purple/35 bg-purple/[0.08] text-purple-2",
  red: "border-st-blocked/30 bg-st-blocked/[0.07] text-ty-bug",
};

const REC_TONE: Record<string, Tone> = { retire: "red", revise: "lime", verify: "purple", keep: "neutral" };

function Label({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn("font-mono text-micro uppercase tracking-[0.06em] text-faint", className)}>{children}</span>;
}

/** The right-hand panel: what the selected row is, why it is in this queue, and its actions. */
export function MemoryDetail({
  row,
  pos,
  decisions,
  onDecide,
  canonId,
  onCanon,
  asked,
}: {
  row: Row;
  pos: string;
  decisions: Decision[];
  onDecide: (d: Decision) => void;
  canonId: string;
  onCanon: (id: string) => void;
  asked?: CandidateJudge;
}) {
  const q = QUEUE_BY_ID[row.queue];
  const s = row.scored;
  const variants = row.cluster ? [row.cluster.representative, ...row.cluster.members] : [];
  const text = variants.find((v) => v.id === canonId)?.text ?? row.text;
  const judge = judgeWords(s);
  const meta: [string, string][] = [
    ["AGENT", row.agent],
    ["SOURCE", row.cluster ? `${variants.length} near-duplicates` : row.source || "—"],
    ["CONFIDENCE", row.conf == null ? "not scored" : `${row.conf.toFixed(2)} · ${confBand(row.conf)}`],
    ...(judge ? [["JUDGE", judge] as [string, string]] : []),
    ...(s && s.reasons.length ? [["EVIDENCE", s.reasons.join(" · ")] as [string, string]] : []),
    ["CLASS", row.shard.scope || "—"],
  ];
  const rec = row.stale?.recommendation;

  return (
    <div data-testid="memory-detail">
      <div className="flex flex-col gap-2.5 border-b border-line-2 px-4 py-3.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-small text-muted">{row.id}</span>
          <span className={cn("rounded-[5px] border px-1.5 py-0.5 font-mono text-micro uppercase tracking-wide", q.tag)}>
            {q.label}
          </span>
          <span className="flex-1" />
          <span className="font-mono text-meta text-faint">{pos}</span>
        </div>
        <p className="whitespace-pre-wrap text-[13.5px] leading-relaxed text-fg">{text}</p>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-small">
          {meta.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="pt-px font-mono text-micro tracking-wide text-faint">{k}</dt>
              <dd className="m-0 text-fg-2">{v}</dd>
            </div>
          ))}
        </dl>
      </div>

      {row.queue === "conflicts" && s && (
        <div className="flex flex-col gap-2 border-b border-line-2 px-4 py-3">
          <Label className="text-ty-bug">Contradicts published</Label>
          {(s.conflicts ?? []).map((c) => (
            <div key={c} className="rounded-[10px] border border-st-blocked/25 bg-st-blocked/[0.04] px-3 py-2 text-body leading-relaxed text-fg-2">
              {c}
            </div>
          ))}
        </div>
      )}

      {row.cluster && (
        <div className="flex flex-col gap-1.5 border-b border-line-2 px-4 py-3" role="radiogroup" aria-label="Canonical wording">
          <Label className="text-st-review">Pick the canonical wording</Label>
          {variants.map((v) => {
            const on = v.id === canonId;
            return (
              <button
                key={v.id}
                role="radio"
                aria-checked={on}
                onClick={() => onCanon(v.id)}
                className={cn(
                  "flex gap-2.5 rounded-[9px] border px-2.5 py-2 text-left hover:border-control-hover",
                  on ? "border-st-review/45 bg-st-review/[0.05]" : "border-control",
                )}
              >
                <span
                  className={cn("mt-[3px] h-3 w-3 flex-none rounded-full border", on ? "border-st-review bg-st-review" : "border-line")}
                  aria-hidden
                />
                <span className="min-w-0 flex-1">
                  <span className="block text-[12px] leading-relaxed text-fg-2">{v.text}</span>
                  <span className="mt-0.5 block font-mono text-meta text-faint">{v.id} · {v.origin || "agent"}</span>
                </span>
              </button>
            );
          })}
        </div>
      )}

      {row.stale && (
        <div className="flex flex-col gap-2.5 border-b border-line-2 px-4 py-3">
          <div>
            <Label>Why it may no longer hold</Label>
            <ul className="mt-1.5 flex flex-col gap-1">
              {row.stale.why.map((w) => (
                <li key={w} className="flex items-baseline gap-2 text-small leading-relaxed text-fg-2">
                  <span className="h-1.5 w-1.5 flex-none rounded-full bg-st-review" aria-hidden />
                  {w}
                </li>
              ))}
            </ul>
          </div>
          {rec ? (
            <div className={cn("flex flex-col gap-1.5 rounded-[10px] border px-3 py-2.5", TONE[REC_TONE[rec.action] ?? "neutral"])}>
              <div className="flex items-center gap-2">
                <Label className="text-muted">Recommend</Label>
                <span className="font-mono text-meta font-semibold uppercase tracking-wide">{rec.action}</span>
                <span className="flex-1" />
                <span className="font-mono text-meta text-faint">conf {rec.confidence.toFixed(2)}</span>
              </div>
              <p className="text-[12px] leading-relaxed text-fg-2">{rec.reason}</p>
            </div>
          ) : (
            <p className="rounded-[10px] border border-line-2 px-3 py-2 text-small text-muted">
              <span className="font-mono text-micro uppercase tracking-wide text-faint">Ungraded</span> — {row.stale.ungraded_reason || "the judge did not answer"}. No recommendation, so nothing to apply.
            </p>
          )}
        </div>
      )}

      {asked && (
        <div className="border-b border-line-2 px-4 py-2.5 text-small">
          {asked.verdict ? (
            <p className="text-fg-2">
              Judge: {Math.round(asked.verdict.quality * 100)}% — {asked.verdict.reason || (asked.verdict.keep ? "publish-worthy" : "not publish-worthy")}
            </p>
          ) : asked.cause === "undecided" ? (
            <span className="rounded border border-line-2 bg-surface-3 px-1.5 py-0.5 font-mono text-micro uppercase tracking-wide text-muted">undecided</span>
          ) : (
            <p className="text-faint">Judge unavailable: {asked.cause_detail}</p>
          )}
        </div>
      )}

      <div className="flex flex-col gap-1.5 px-4 py-3">
        {decisions.map((d) => (
          <button
            key={d.id}
            onClick={() => onDecide(d)}
            disabled={!!d.disabled}
            title={d.disabled}
            className={cn(
              "flex items-center gap-2.5 rounded-[9px] border px-3 py-2 text-left transition-[filter] hover:brightness-125 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:brightness-100",
              TONE[d.tone],
              d.tone === "neutral" && "border-control",
            )}
          >
            <span className="min-w-0 flex-1">
              <span className="block text-body font-semibold">{d.label}</span>
              <span className="mt-px block text-small text-muted">{d.disabled ?? d.sub}</span>
            </span>
            {d.key && (
              <kbd className="rounded border border-line-2 px-1.5 font-mono text-meta text-faint">{d.key}</kbd>
            )}
          </button>
        ))}
      </div>
    </div>
  );
}
