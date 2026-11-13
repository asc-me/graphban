import { useState } from "react";

import { copyText } from "@/lib/clipboard";
import type { DrainNote as DrainNoteData } from "@/lib/types";

export const DRAIN_UNKNOWN = "Could not check for a running drain.";

/** Anything but live / not_live is a failed read: never "no drain is running". */
export function drainStatusOf(drain: Partial<DrainNoteData> | null | undefined): "live" | "not_live" | "unknown" {
  return drain?.status === "live" || drain?.status === "not_live" ? drain.status : "unknown";
}

/** The Processed line's compact form: a live drain's message joins the result sentence. */
export function DrainNote({
  status, prd, message, command, compact = false,
}: {
  status?: string | null;
  prd?: string | null;
  message?: string | null;
  command?: string | null;
  compact?: boolean;
}) {
  const s = drainStatusOf({ status } as Partial<DrainNoteData>);
  const [copyFailed, setCopyFailed] = useState(false);
  const showProjectWide = (s === "live" || s === "not_live") && !prd;
  const tag = prd ? prd : showProjectWide ? "PROJECT-WIDE · NO PRD" : null;
  const box = s === "unknown" ? "border border-dashed border-red-500/50 bg-red-500/[0.06]" : "border border-line bg-surface";

  return (
    <div data-testid="drain-block" data-status={s} className={compact ? "mt-1" : "mt-1.5"}>
      {!compact && (
        <div className="flex items-center gap-2 font-mono text-micro uppercase tracking-[0.07em] text-faint-2">
          <span>DRAIN</span>
          {tag && (
            <span data-testid="drain-tag" className="rounded border border-line px-1.5 py-px text-muted">{tag}</span>
          )}
        </div>
      )}
      <div className={`mt-1.5 flex items-start gap-2 rounded-md px-3 py-2 ${box}`}>
        {s === "live" && <span aria-hidden className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-st-done" />}
        {s === "not_live" && (
          <span aria-hidden className="mt-1.5 h-2 w-2 shrink-0 rounded-full border border-faint-2" />
        )}
        {s === "unknown" && <span aria-hidden className="shrink-0 text-red-500">⊗</span>}
        <div className="min-w-0 flex-1">
          <p data-testid="drain-note" className="text-body text-fg-2">
            {s === "unknown" ? DRAIN_UNKNOWN : message}
          </p>
          {s === "not_live" && command && (
            <>
              <p className="mt-1.5 text-small text-muted">To start one, run this in a terminal:</p>
              <div className="mt-1 flex items-center gap-2">
                <code className="select-all rounded-[5px] border border-line bg-bg px-1.5 py-px font-mono text-meta text-fg-2">
                  {`$ ${command}`}
                </code>
                <button
                  onClick={() => void copyText(command).then((ok) => setCopyFailed(!ok))}
                  className="rounded border border-control px-1.5 py-px text-small text-muted hover:text-fg"
                >
                  Copy
                </button>
              </div>
              {copyFailed && (
                <p role="alert" className="mt-1 text-small text-muted">
                  Couldn't copy. Select the command and copy it yourself.
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
