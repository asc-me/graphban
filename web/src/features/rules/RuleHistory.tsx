/** A rule's change history (GRPH-1598): the last change, and the full trail on request. */
import * as React from "react";

import { useProjectCtx } from "@/features/ProjectContext";
import { useRuleHistory } from "@/lib/queries";
import { relTime } from "@/lib/relTime";
import type { Event } from "@/lib/types";

const note = "text-[11.5px] leading-snug text-muted";

function line(e: Event): string {
  const why = typeof e.meta?.reason === "string" ? e.meta.reason : "";
  return `${e.actor_label || e.actor_id} ${relTime(e.ts) ?? "at an unknown time"}${why ? ` — “${why}”` : ""}`;
}

export function RuleHistory({ ruleId }: { ruleId: string }) {
  const { activeId } = useProjectCtx();
  const [open, setOpen] = React.useState(false);
  const { data } = useRuleHistory(activeId, ruleId, true);
  if (!data || data.length === 0) return null;
  const [last, ...rest] = data;
  return (
    <div className="flex flex-col gap-1" data-testid="rules-history">
      <span className={note} data-testid="rules-toggle-changed">
        Changed by {line(last)}
      </span>
      {rest.length > 0 && (
        <button type="button" className="self-start text-[11.5px] text-accent underline" onClick={() => setOpen(!open)}>
          {open ? "Hide history" : `Show full history (${data.length})`}
        </button>
      )}
      {open && (
        <ol className="flex flex-col gap-0.5" data-testid="rules-history-trail">
          {rest.map((e) => (
            <li key={e.id} className={note}>
              {line(e)}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
