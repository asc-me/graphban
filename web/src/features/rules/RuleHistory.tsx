/** A rule's change history (GRPH-1598): the last change, and the full trail on request. */
import * as React from "react";

import { useProjectCtx } from "@/features/ProjectContext";
import { useAdoptedProposals, useProposalDecisions, useRuleHistory } from "@/lib/queries";
import { relTime } from "@/lib/relTime";
import type { Event, ProposalDecision, RuleProposal } from "@/lib/types";

const note = "text-[11.5px] leading-snug text-muted";

function line(e: Event): string {
  const why = typeof e.meta?.reason === "string" ? e.meta.reason : "";
  return `${e.actor_label || e.actor_id} ${relTime(e.ts) ?? "at an unknown time"}${why ? ` — “${why}”` : ""}`;
}

/** "adopted by Alex · 2d ago, proposed by Agent x (cited item GRPH-5 +1)": the agent is named only here.
 * Who and when come from the proposal's `adopt_rule_proposal` event; the list route does not send them. */
export function adoptedLine(p: RuleProposal, d: ProposalDecision | undefined): string {
  const first = p.citations[0];
  const ref = first ? (first.kind === "file" ? first.ref.path : `${first.kind} ${first.ref}`) : "";
  const more = p.citations.length > 1 ? ` +${p.citations.length - 1}` : "";
  const who = p.proposer_kind === "agent" ? `Agent ${p.proposer}` : p.proposer;
  const when = d?.at ? relTime(d.at) : null;
  return `adopted${d ? ` by ${d.by}` : ""}${when ? ` · ${when}` : ""}, proposed by ${who}${ref ? ` (cited ${ref}${more})` : ""}`;
}

export function RuleHistory({ ruleId }: { ruleId: string }) {
  const { activeId } = useProjectCtx();
  const [open, setOpen] = React.useState(false);
  const { data } = useRuleHistory(activeId, ruleId, true);
  const { data: adopted } = useAdoptedProposals(activeId);
  const { data: adoptions } = useProposalDecisions(activeId, "adopt");
  const origin = (adopted ?? []).find((p) => p.rule_id === ruleId);
  if (!data || data.length === 0) {
    return origin ? (
      <span className={note} data-testid="rules-history-adopted">
        {adoptedLine(origin, adoptions?.get(origin.id))}
      </span>
    ) : null;
  }
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
      {origin && (
        <span className={note} data-testid="rules-history-adopted">
          {adoptedLine(origin, adoptions?.get(origin.id))}
        </span>
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
