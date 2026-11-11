import { cn } from "@/lib/cn";
import type { HarnessBurnDown, HarnessCapacityEntitlement } from "@/lib/types";
import { STATE, fmtTokens, shortDay, shortWhen } from "./capacityLook";

type Placed = Extract<HarnessBurnDown, { available: true }>;

const W = 300, H = 96, BASE = 93, SPAN = 90;

const TONE = {
  early: { line: "#f0b450", fill: "rgba(240,180,80,0.1)", border: "border-[rgba(240,180,80,0.35)]", text: "text-[#f0b450]" },
  ok: { line: "#5fd07a", fill: "rgba(95,208,122,0.1)", border: "border-line-2", text: "text-st-done" },
  stopped: { line: "#5c656e", fill: "rgba(92,101,110,0.12)", border: "border-[rgba(255,107,107,0.3)]", text: "text-st-blocked" },
  quiet: { line: "#9aa3ab", fill: "rgba(154,163,171,0.08)", border: "border-line-2", text: "text-muted" },
} as const;

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

/** The verdict sentence and its tone. Every verdict is the server's; this only words it. */
export function burnVerdict(b: Placed): { text: string; tone: keyof typeof TONE } {
  const f = b.forecast;
  const stepMs = b.step === "hour" ? 3_600_000 : 86_400_000;
  switch (f.verdict) {
    case "runs_out_early": {
      const early = Math.max(1, Math.round((Date.parse(b.end) - Date.parse(f.runs_out_at!)) / stepMs));
      const at = b.step === "hour" ? shortWhen(f.runs_out_at!) : shortDay(f.runs_out_at!);
      return { text: `Runs out ~${at} — ${plural(early, b.step)} before reset`, tone: "early" };
    }
    case "run_out":
      return { text: `Ran out${f.runs_out_at ? ` ${shortWhen(f.runs_out_at)}` : ""} — nothing left until the reset`, tone: "early" };
    case "stopped":
      return { text: `Burn stopped${f.stopped_at ? ` at ${shortWhen(f.stopped_at)}` : ""} — spawns are failing, not spending.`, tone: "stopped" };
    case "no_spend":
      return { text: "No reported spend this period — nothing to forecast", tone: "quiet" };
    default:
      return { text: `On track — resets with ~${fmtTokens(f.left_at_reset ?? b.left)} unused`, tone: "ok" };
  }
}

/** One entitlement's budget period: left over time, the forecast dashed, an even burn for reference. */
export function BurnCard({ e }: { e: HarnessCapacityEntitlement & { burn_down: Placed } }) {
  const b = e.burn_down;
  const f = b.forecast;
  const t0 = Date.parse(b.start), t1 = Date.parse(b.end);
  const X = (iso: string) => Math.min(W, Math.max(0, ((Date.parse(iso) - t0) / (t1 - t0)) * W));
  const Y = (left: number) => BASE - (Math.max(0, Math.min(left, b.budget)) / b.budget) * SPAN;
  const pts = b.series.map((p) => [X(p.at), Y(p.left)] as const);
  const now = b.series[b.series.length - 1];
  const nowX = X(now.at);
  const fc: (readonly [number, number])[] = [];
  if (f.verdict === "runs_out_early" && f.runs_out_at) fc.push([nowX, Y(b.left)], [X(f.runs_out_at), Y(0)]);
  else if (f.verdict === "on_track" || f.verdict === "no_spend") fc.push([nowX, Y(b.left)], [W, Y(f.left_at_reset ?? b.left)]);
  const v = burnVerdict(b);
  const tone = TONE[v.tone];
  const join = (ps: readonly (readonly [number, number])[]) => ps.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const outX = f.verdict === "runs_out_early" && f.runs_out_at ? X(f.runs_out_at) : null;
  const step = b.step;
  const rate = f.verdict === "stopped"
    ? `avg ${fmtTokens(f.avg_rate)}/${step} before failing`
    : `last ${plural(f.recent_steps, step)} ${fmtTokens(f.recent_rate)}/${step} · avg ${fmtTokens(f.avg_rate)}/${step}`;
  const axis = (iso: string) => (step === "hour" ? shortWhen(iso, new Date(iso)) : shortDay(iso));

  return (
    <div
      data-testid="capacity-burn"
      data-entitlement={e.id}
      data-verdict={f.verdict}
      className={cn("flex flex-col gap-2.5 rounded-[11px] border bg-surface-2 px-3.5 py-3", tone.border)}
    >
      <div className="flex items-start gap-2">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="break-all font-mono text-[12px] text-fg">{e.id}</span>
          <span className="font-mono text-[10.5px] text-faint">{b.period} · {fmtTokens(b.budget)} budget</span>
        </div>
        <StatePill state={e.state} />
      </div>
      <div className="flex items-baseline gap-1.5">
        <span className={cn("font-mono text-[22px] font-medium tracking-[-0.02em]", e.state === "broken" ? "text-muted" : "text-fg")}>
          {fmtTokens(b.left)}
        </span>
        <span className="font-mono text-small text-muted">left of {fmtTokens(b.budget)}</span>
        {b.unreported > 0 && (
          <span data-testid="capacity-burn-unreported" className="font-mono text-small text-faint" title="Spawns with no spend report: the real figure is lower">
            +{b.unreported} unreported
          </span>
        )}
      </div>
      <div className="relative h-24">
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" width="100%" height={H} className="block overflow-visible" aria-hidden="true">
          <line x1="0" y1={BASE} x2={W} y2={BASE} stroke="#1e242a" strokeWidth="1" vectorEffect="non-scaling-stroke" />
          <line x1="0" y1={BASE - SPAN} x2={W} y2={BASE} stroke="#2c353c" strokeWidth="1" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
          <polygon points={join([[0, BASE], ...pts, [nowX, BASE]])} fill={tone.fill} />
          <line x1={nowX} y1="0" x2={nowX} y2={H} stroke="#2a333a" strokeWidth="1" vectorEffect="non-scaling-stroke" />
          {fc.length > 0 && (
            <polyline data-testid="capacity-burn-forecast" points={join(fc)} fill="none" stroke={tone.line} strokeWidth="1.6" strokeDasharray="4 3" vectorEffect="non-scaling-stroke" />
          )}
          <polyline data-testid="capacity-burn-actual" points={join(pts)} fill="none" stroke={tone.line} strokeWidth="2" vectorEffect="non-scaling-stroke" />
        </svg>
        <span className="absolute -top-0.5 -translate-x-1/2 rounded-[3px] bg-surface-2 px-1 font-mono text-[9px] text-muted" style={{ left: `${(nowX / W) * 100}%` }}>
          now
        </span>
        {outX !== null && (
          <span
            data-testid="capacity-burn-out"
            className="absolute -bottom-px size-[7px] -translate-x-1/2 rounded-full shadow-[0_0_0_3px_var(--color-surface-2)]"
            style={{ left: `${(outX / W) * 100}%`, background: tone.line }}
          />
        )}
      </div>
      <div className="flex justify-between font-mono text-meta text-[#4a525a]">
        <span>{axis(b.start)}</span>
        <span>reset {axis(b.end)}</span>
      </div>
      <div className="flex flex-col gap-[3px] border-t border-line-2/70 pt-2">
        <span data-testid="capacity-burn-verdict" className={cn("text-[12px] font-medium", tone.text)}>{v.text}</span>
        <span className="font-mono text-[10.5px] text-muted">{rate}</span>
      </div>
    </div>
  );
}

export function StatePill({ state, className }: { state: HarnessCapacityEntitlement["state"]; className?: string }) {
  const s = STATE[state];
  return (
    <span
      data-testid="capacity-state"
      data-state={state}
      title={s.detail}
      className={cn(
        "inline-flex h-5 flex-none items-center gap-[5px] rounded-[6px] border px-[7px] font-mono text-meta font-semibold tracking-[0.5px]",
        s.pill, s.text, className,
      )}
    >
      <span aria-hidden="true">{s.glyph}</span>
      {s.word}
    </span>
  );
}
