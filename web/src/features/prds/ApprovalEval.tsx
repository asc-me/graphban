import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, CircleDashed, Minus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { PrdEval, PrdEvalCompleteness } from "@/lib/types";

/** Pre-approval quality next to the grill (GRPH-80).

    Completeness and coverage gaps load cheaply. Ambiguity/testability wait
    for Ask the judge. Ungraded is named, never a quiet fail. The grill still
    approves on its own — this warns, it does not block. */

const STATE_META = {
  present: { icon: Check, color: "#5fd07a", label: "present" },
  thin: { icon: Minus, color: "#e0b34a", label: "placeholder" },
  missing: { icon: CircleDashed, color: "#ff6b6b", label: "missing" },
} as const;

export function ApprovalEval({
  prdId,
  grillComplete,
}: {
  prdId: string;
  grillComplete: boolean;
}) {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["prd-evaluate", prdId],
    queryFn: () => api.prdEvaluate(prdId),
    enabled: !!prdId,
  });
  const ask = useMutation({
    mutationFn: () => api.prdEvaluateAsk(prdId),
    onSuccess: (row) => qc.setQueryData(["prd-evaluate", prdId], row),
  });

  if (!data) {
    return <p className="text-[12px] text-faint">Loading readiness…</p>;
  }

  return (
    <div className="rounded-[11px] border border-line-2 bg-surface-2/60 p-3">
      <div className="mb-2.5 flex items-center gap-2">
        <span className="font-mono text-[10px] uppercase tracking-wide text-faint">
          Readiness to approve
        </span>
        <ReadyChip data={data} />
      </div>

      <div className="flex flex-col gap-1.5">
        {data.completeness.map((row) => (
          <CompletenessRow key={row.dimension} row={row} />
        ))}
      </div>

      <p className="mt-2 text-[11px] leading-snug text-faint">{data.coverage_note}</p>
      {data.coverage_gaps.length > 0 && (
        <p className="mt-1 text-[11px] text-[#e0b34a]">
          No work linked: {data.coverage_gaps.join(" · ")}
        </p>
      )}
      {data.shaped && data.empty_sections.length > 0 && (
        <p className="mt-1 text-[11px] text-[#e0b34a]">
          Empty headings: {data.empty_sections.join(" · ")}
        </p>
      )}

      {data.judged ? (
        <JudgedBlock data={data} />
      ) : (
        <p className="mt-2 font-mono text-[10.5px] text-faint">
          not judged — {data.ungraded_reason}
        </p>
      )}

      {(data.ambiguous.length > 0 || data.untestable.length > 0) && (
        <ul className="mt-2 space-y-1 text-[11.5px] text-fg-2">
          {data.ambiguous.map((c) => (
            <li key={`a-${c}`}>Ambiguous: {c}</li>
          ))}
          {data.untestable.map((c) => (
            <li key={`t-${c}`}>Untestable: {c}</li>
          ))}
        </ul>
      )}

      {data.callouts.length > 0 && data.judged === false && (
        <ul className="mt-2 space-y-1 text-[11.5px] text-fg-2">
          {data.callouts.slice(0, 6).map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      )}

      <div className="mt-2.5 flex items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={() => ask.mutate()}
          disabled={ask.isPending}
        >
          {ask.isPending ? "Asking…" : "Ask the judge"}
        </Button>
      </div>

      {grillComplete && <GrillWarning data={data} />}
    </div>
  );
}

function ReadyChip({ data }: { data: PrdEval }) {
  if (!data.judged) {
    return (
      <span className="rounded border border-line-2 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide text-faint">
        ungraded
      </span>
    );
  }
  return (
    <span
      className={cn(
        "rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide",
        data.ready
          ? "border-line-2 bg-surface-3 text-muted"
          : "border-[#3a2f1a] bg-[rgba(224,179,74,0.08)] text-[#e0b34a]",
      )}
    >
      {data.ready ? "ready" : "not ready"}
    </span>
  );
}

function CompletenessRow({ row }: { row: PrdEvalCompleteness }) {
  const meta = STATE_META[row.state];
  const Icon = meta.icon;
  return (
    <div className="flex items-center gap-2 text-[12px]">
      <Icon size={13} className="flex-none" style={{ color: meta.color }} />
      <span className="flex-none text-fg-2">{row.label}</span>
      <span className="font-mono text-[10px] uppercase tracking-wide" style={{ color: meta.color }}>
        {meta.label}
      </span>
      {row.section && row.state !== "present" && (
        <span className="min-w-0 truncate text-[11px] text-faint">{row.section}</span>
      )}
    </div>
  );
}

function JudgedBlock({ data }: { data: PrdEval }) {
  if (data.judge_reason) {
    return <p className="mt-2 text-[11.5px] text-faint">{data.judge_reason}</p>;
  }
  return null;
}

function GrillWarning({ data }: { data: PrdEval }) {
  if (!data.judged) {
    return (
      <p className="mt-2.5 flex items-start gap-1.5 text-[11px] leading-snug text-faint">
        <AlertTriangle size={12} className="mt-0.5 flex-none" />
        The grill approved this; the quality judge was not asked — ungraded is not a pass.
      </p>
    );
  }
  if (data.ready === false) {
    return (
      <p className="mt-2.5 flex items-start gap-1.5 text-[11px] leading-snug text-[#e0b34a]">
        <AlertTriangle size={12} className="mt-0.5 flex-none" />
        The grill approved this; the judge still flagged issues. Approval is not blocked.
      </p>
    );
  }
  return null;
}
