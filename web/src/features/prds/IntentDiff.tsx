import { AlertTriangle, ArrowRight, Minus, Plus, Pencil } from "lucide-react";

import { cn } from "@/lib/cn";
import type { IntentDiff as IntentDiffData, IntentDiffSection } from "@/lib/types";

/** What a rebaseline would actually change (GRPH-317).
 *
 *  PRD-12 is blunt about why this exists: without it "the human ratifies a decision
 *  already made in chat without seeing its effect on the spec, and it is rubber-stamping
 *  with an audit trail."
 *
 *  So it sits ABOVE the grill rather than behind a tab. A reviewer who has to go looking
 *  for the diff is a reviewer who approves without it — and the one thing this surface
 *  cannot afford is being skippable. */

const STATE = {
  modified: { icon: Pencil, color: "#e0b34a", label: "changed" },
  added: { icon: Plus, color: "#5fd07a", label: "added" },
  removed: { icon: Minus, color: "#ff6b6b", label: "REMOVED" },
  renamed: { icon: ArrowRight, color: "#7ca2ff", label: "renamed" },
  unchanged: { icon: null, color: "#8b949e", label: "" },
} as const;

export function IntentDiff({ diff }: { diff: IntentDiffData }) {
  // Only meaningful while a rebaseline is being decided. Outside that, divergence from
  // the baseline is drift — reportable, but not something anyone is approving here.
  if (!diff.governed || !diff.pending) return null;

  const changed = diff.sections.filter((s) => s.state !== "unchanged");
  const unchanged = diff.sections.length - changed.length;

  return (
    <div className="rounded-[11px] border border-[rgba(224,179,74,0.3)] bg-[rgba(224,179,74,0.05)] p-3">
      <div className="mb-2 flex items-center gap-2">
        <AlertTriangle size={13} className="text-[#e0b34a]" />
        <span className="font-mono text-[10px] uppercase tracking-wide text-[#e0b34a]">
          Rebaseline proposed · {changed.length} section{changed.length === 1 ? "" : "s"} changing
        </span>
      </div>

      <p className="mb-3 text-[11.5px] leading-snug text-fg-2">
        <span className="font-mono text-[10px] uppercase text-faint">
          {diff.pending.reason_type}
        </span>{" "}
        — {diff.pending.reason}
        <span className="mt-0.5 block text-[10.5px] text-faint">
          Requested by {diff.pending.requested_by}. Approving replaces{" "}
          {diff.baseline_version} as the intent everything is measured against.
        </span>
      </p>

      {changed.length === 0 ? (
        <p className="text-[11.5px] text-faint">
          Nothing has changed yet. Edit the spec, then finish the grill.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          {changed.map((s) => (
            <Section key={`${s.state}-${s.title}`} section={s} />
          ))}
        </div>
      )}

      {unchanged > 0 && (
        <p className="mt-2 font-mono text-[10px] text-faint">
          {unchanged} section{unchanged === 1 ? "" : "s"} unchanged
        </p>
      )}
    </div>
  );
}

function Section({ section }: { section: IntentDiffSection }) {
  const meta = STATE[section.state] ?? STATE.unchanged;
  const Icon = meta.icon;
  return (
    <div className="overflow-hidden rounded-[9px] border border-line-2 bg-surface-2">
      <div className="flex items-center gap-2 border-b border-line-2 px-2.5 py-1.5">
        {Icon && <Icon size={11} style={{ color: meta.color }} />}
        <span className="min-w-0 flex-1 truncate text-[12px] text-fg-2">
          {section.was && (
            <span className="text-faint line-through">{section.was} </span>
          )}
          {section.title}
        </span>
        <span
          className="font-mono text-[9px] uppercase tracking-wide"
          style={{ color: meta.color }}
        >
          {meta.label}
        </span>
      </div>

      {section.lines && section.lines.length > 0 && (
        // Horizontally scrollable rather than wrapped: a wrapped diff line reads as two
        // changes, and the whole point is that the reader counts what moved correctly.
        <div className="max-h-64 overflow-auto">
          {section.lines.map((l, i) => (
            <div
              key={i}
              className={cn(
                "whitespace-pre px-2.5 font-mono text-small leading-[1.55]",
                l.op === "+" && "bg-[rgba(95,208,122,0.1)] text-st-done",
                l.op === "-" && "bg-[rgba(255,107,107,0.09)] text-st-blocked",
                l.op === "=" && "text-faint",
              )}
            >
              {l.op === "=" ? "  " : `${l.op} `}
              {l.text || " "}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
