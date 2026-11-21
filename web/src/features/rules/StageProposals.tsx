/**
 * One row per stage: how many proposals are open for it, with a link to the proposals strip
 * (GRPH-1807). No Adopt or Dismiss lives here. Custom rules for the stage are listed below it;
 * a rule's "by" names the PERSON who saved or adopted it, never the agent that proposed it.
 */

import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { useAdoptedProposals, useCustomRules, useProposalDecisions, useRuleProposals } from "@/lib/queries";
import type { CustomRule, ProposalDecision, RuleProposal, RuleProposalCitation } from "@/lib/types";

const MONO = "font-mono";

export function Citation({ c }: { c: RuleProposalCitation }) {
  if (c.kind === "file") {
    const { path, commit, lines } = c.ref;
    return (
      <span className={MONO} data-testid="citation-file">
        {path}
        {lines ? `:${lines}` : ""} @ {commit.slice(0, 12)}
      </span>
    );
  }
  if (c.kind === "url") {
    return (
      <a href={c.ref} target="_blank" rel="noreferrer" className="text-accent underline" data-testid="citation-url">
        {c.ref}
      </a>
    );
  }
  return (
    <span className={MONO} data-testid={`citation-${c.kind}`}>
      {c.kind} {c.ref}
    </span>
  );
}

/** Who saved or adopted a custom rule: the person on its `adopt_rule_proposal` event, else the
 * rule's author (always the person who saved it — an agent key cannot). Never the proposer. */
export function adopterOf(
  r: CustomRule,
  adopted: RuleProposal[],
  decisions: Map<number, ProposalDecision> | undefined,
): string | null {
  const a = adopted.find((p) => p.rule_id === r.rule_id);
  const d = a ? decisions?.get(a.id) : undefined;
  return d?.by ?? r.author;
}

function CustomRuleRow({ r, by }: { r: CustomRule; by: string | null }) {
  return (
    <li className="py-1" data-testid={`custom-rule-${r.rule_id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={cn(MONO, "rounded-[6px] border border-control px-1 text-meta text-fg-2")}>Custom</span>
        <span className="text-body font-semibold text-fg">{r.title}</span>
        <span className="text-small text-muted">by {by ?? "unknown"}</span>
      </div>
      <p className="text-small text-fg-2">Why: {r.why}</p>
    </li>
  );
}

export function StageProposals({
  stageId,
  ruleIds,
  onReview,
}: {
  stageId: string;
  ruleIds: string[];
  /** Opens the proposals strip filtered to this stage. */
  onReview?: (stageId: string) => void;
}) {
  const { activeId } = useProjectCtx();
  const proposals = useRuleProposals(activeId);
  const { data: custom } = useCustomRules(activeId);
  const { data: adopted } = useAdoptedProposals(activeId);
  const { data: adoptions } = useProposalDecisions(activeId, "adopt");

  // The API carries the primitive's capitalised stage ("Plan"); RulesView's stage ids are lowercase.
  const here = stageId.toLowerCase();
  const sameStage = (s: unknown) => typeof s === "string" && s.toLowerCase() === here;
  const mine = (proposals.data ?? []).filter(
    (p) => p.status === "open" && (sameStage(p.draft?.stage) || ruleIds.includes(p.rule_id)),
  );
  const customHere = (custom ?? []).filter((r) => sameStage(r.stage));
  const failed = proposals.isError;
  return (
    <div className="flex-none max-h-[45%] overflow-y-auto border-b border-line px-4 py-2" data-testid="stage-proposals">
      <div className="flex flex-wrap items-center gap-2 text-small text-fg-2" data-testid="stage-proposals-count">
        <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>PROPOSALS</span>
        {failed ? (
          <>
            <span>—</span>
            <span>Proposals could not be loaded ·</span>
            <button type="button" className="text-accent underline" onClick={() => void proposals.refetch()}>
              Retry
            </button>
          </>
        ) : mine.length === 0 ? (
          <span>{proposals.isPending ? "…" : "No proposals"}</span>
        ) : (
          <>
            <span>{mine.length} proposed ·</span>
            <button type="button" className="text-accent underline" onClick={() => onReview?.(stageId)}>
              Review in proposals
            </button>
          </>
        )}
      </div>
      {customHere.length > 0 && (
        <>
          <h3 className={cn(MONO, "mt-2 text-meta tracking-[0.07em] text-muted-2")}>CUSTOM RULES</h3>
          <ul className="list-none">
            {customHere.map((r) => (
              <CustomRuleRow key={r.rule_id} r={r} by={adopterOf(r, adopted ?? [], adoptions)} />
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
