import { AlertTriangle, Check, CircleDashed, PauseCircle } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";
import { useGrillDefer } from "@/lib/queries";
import type { GrillDimensionState, GrillState } from "@/lib/types";

/** How approval was earned (AL-301 / PRD-15 D7).
 *
 *  `approved` is reached, not picked, so the editor has to show the WORK rather than a
 *  chosen value: which of the four dimensions were answered, which the author
 *  deliberately left open, and what is still outstanding.
 *
 *  It also shows what set the bar. On the shipped default (`CHAT_PROVIDER=stub`) that is
 *  a mechanical rule — answers counted, substance not assessed — and a reader who cannot
 *  see that would take a stub-graded approval for a judged one. */

const DIMENSION_LABEL: Record<string, string> = {
  scope_edges: "Scope edges",
  failure_modes: "Failure modes",
  contracts: "Contracts",
  open_decisions: "Open decisions",
};

const OUTCOME_META = {
  resolved: { icon: Check, color: "#5fd07a", label: "answered" },
  deferred: { icon: PauseCircle, color: "#e0b34a", label: "deferred" },
  unanswered: { icon: CircleDashed, color: "#8b949e", label: "open" },
} as const;

export function GrillProgress({ state, prdId }: { state: GrillState; prdId: string }) {
  const names = Object.keys(state.dimensions);
  const defer = useGrillDefer(prdId);
  const [deferring, setDeferring] = React.useState<string | null>(null);
  if (!names.length) return null;

  // Only the stub's mechanical bar is worth calling out — a real provider grading is the
  // expected case and does not need a warning.
  const stubGraded = names.filter((n) => state.dimensions[n].graded_by === "stub").length;

  return (
    <div className="rounded-[11px] border border-line-2 bg-surface-2/60 p-3">
      <div className="mb-2.5 flex items-center gap-2">
        <span className="font-mono text-[10px] uppercase tracking-wide text-faint">
          {state.complete ? "Approved by grilling" : "Grill progress"}
        </span>
        {!state.complete && (
          <span className="font-mono text-[10px] text-faint">
            {names.length - state.outstanding.length}/{names.length}
          </span>
        )}
      </div>

      {/* The round was not judged, so the rows below are the PREVIOUS round's verdicts
          (GRPH-485). Said before them, because a reader who takes them for current will
          answer again into the same void — which is the loop this exists to break. */}
      {!state.graded && (
        <div className="mb-2.5 flex gap-2 rounded-[9px] border border-[#4a3a12] bg-[rgba(224,179,74,0.08)] px-2.5 py-2">
          <AlertTriangle size={13} className="mt-0.5 flex-none text-[#e0b34a]" />
          <p className="text-[11px] leading-snug text-fg-2">
            <span className="font-medium text-[#e0b34a]">Not judged this round.</span>{" "}
            {state.ungraded_reason ||
              "the grader could not be asked, so the outcomes below are the previous round's."}
          </p>
        </div>
      )}

      {/* Only when the round WAS graded. An ungraded round has already explained itself
          above, and the standstill is that outage's consequence — saying it twice would
          point at the author's answers for something the grader did. */}
      {state.graded && state.stall?.stalled && !state.complete && (
        <div className="mb-2.5 rounded-[9px] border border-line-2 bg-surface-3/40 px-2.5 py-2">
          <p className="text-[11px] leading-snug text-fg-2">
            <span className="font-medium">
              {state.stall.answers_since_progress} answers, nothing moved.
            </span>{" "}
            Another answer on the same ground is unlikely to land. Defer a dimension with
            a reason, or check the project&rsquo;s chat model.
          </p>
        </div>
      )}

      <div className="flex flex-col gap-1.5">
        {names.map((name) => (
          <DimensionRow
            key={name}
            name={name}
            state={state.dimensions[name]}
            deferring={deferring === name}
            onDefer={() => setDeferring(name)}
            onCancel={() => setDeferring(null)}
            onConfirm={(reason) => {
              defer.mutate({ dimension: name, reason });
              setDeferring(null);
            }}
            pending={defer.isPending}
          />
        ))}
      </div>

      {state.complete ? (
        <p className="mt-2.5 text-[11px] leading-snug text-faint">
          Approved because every dimension was answered or explicitly deferred — not by
          anyone setting a status.
          {stubGraded > 0 && (
            <> Graded offline: answers were recorded, their substance was not assessed.</>
          )}
        </p>
      ) : (
        <p className="mt-2.5 text-[11px] leading-snug text-faint">
          Answer the open dimensions in the grill — or defer one deliberately — and this
          PRD approves itself.
        </p>
      )}
    </div>
  );
}

function DimensionRow({
  name, state, deferring, onDefer, onCancel, onConfirm, pending,
}: {
  name: string;
  state: GrillDimensionState;
  deferring: boolean;
  onDefer: () => void;
  onCancel: () => void;
  onConfirm: (reason: string) => void;
  pending: boolean;
}) {
  const meta = OUTCOME_META[state.outcome] ?? OUTCOME_META.unanswered;
  const Icon = meta.icon;
  const [reason, setReason] = React.useState("");

  // Deferring is the AUTHOR's decision, never the model's inference, so it is a control
  // the author operates — and the reason is required, because a deferral with no words
  // behind it is the hand-waving the standard exists to catch. It rides onto the
  // baseline as the note on this dimension.
  if (deferring) {
    return (
      <div className="flex items-center gap-1.5">
        <input
          autoFocus
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && reason.trim()) onConfirm(reason.trim());
            if (e.key === "Escape") onCancel();
          }}
          placeholder={`Why is ${(DIMENSION_LABEL[name] ?? name).toLowerCase()} being left open?`}
          className="min-w-0 flex-1 rounded-md border border-line-2 bg-surface-2 px-2 py-1 text-[11.5px] outline-none placeholder:text-faint focus:border-line-hover"
        />
        <button
          onClick={() => onConfirm(reason.trim())}
          disabled={!reason.trim() || pending}
          className="flex-none rounded-md border border-line-2 px-2 py-1 font-mono text-[9.5px] uppercase text-[#e0b34a] transition-colors hover:bg-surface-3 disabled:opacity-40"
        >
          Defer
        </button>
        <button
          onClick={onCancel}
          className="flex-none rounded-md px-1.5 py-1 font-mono text-[9.5px] uppercase text-faint transition-colors hover:text-fg-2"
        >
          Cancel
        </button>
      </div>
    );
  }

  return (
    <div className="flex items-start gap-2 text-[12px]">
      <Icon size={13} className="mt-0.5 flex-none" style={{ color: meta.color }} />
      <span className="flex-none text-fg-2">{DIMENSION_LABEL[name] ?? name}</span>
      <span className="font-mono text-[10px] uppercase tracking-wide" style={{ color: meta.color }}>
        {meta.label}
      </span>
      {state.note && (
        <span className="min-w-0 flex-1 truncate text-[11px] text-faint" title={state.note}>
          {state.note}
        </span>
      )}
      {state.graded_by === "stub" && (
        <span
          title="Graded offline: an answer was recorded, its substance was not assessed."
          className="ml-auto flex-none rounded border border-line-2 px-1 font-mono text-[9px] uppercase text-faint"
        >
          offline
        </span>
      )}
      {state.outcome === "unanswered" && (
        <button
          onClick={onDefer}
          disabled={pending}
          title="Leave this dimension deliberately open, with a reason. Deferring completes the grill; it does not skip it."
          className="ml-auto flex-none rounded border border-line-2 px-1 font-mono text-[9px] uppercase text-faint transition-colors hover:text-[#e0b34a] disabled:opacity-40"
        >
          defer
        </button>
      )}
    </div>
  );
}

/** The status control's replacement for a selectable `approved` — it explains why the
 *  option is absent rather than leaving a reader to wonder where it went. */
export function ApprovedIsEarned({ complete }: { complete: boolean }) {
  return (
    <div
      className={cn(
        "border-t border-line px-2 py-1.5 font-mono text-[9.5px] leading-snug",
        complete ? "text-[#5fd07a]" : "text-faint",
      )}
    >
      {complete
        ? "APPROVED — reached by finishing the grill"
        : "APPROVED is reached by finishing the grill"}
    </div>
  );
}
