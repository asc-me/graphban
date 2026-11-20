/**
 * Rule proposals inbox and custom rules for one stage (GRPH-1781 / P91 S9). Open proposals show
 * kind, what they change, proposer, citations and a LOOSENS flag. A person selects proposals and
 * adopts them under one reason; a loosening cannot be selected for the batch and has its own
 * Adopt with its own reason. Dismiss needs a reason. Refusals are shown verbatim.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { useAdoptRuleProposals, useCustomRules, useDismissRuleProposal, useRuleProposals } from "@/lib/queries";
import type { CustomRule, RuleProposal, RuleProposalCitation } from "@/lib/types";

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

function summary(p: RuleProposal): string {
  const title = typeof p.draft?.title === "string" ? p.draft.title : "";
  if (title) return title;
  const params = Object.keys(p.params ?? {}).length ? JSON.stringify(p.params) : "";
  return params ? `${p.rule_id} → ${params}` : p.rule_id;
}

function ProposalRow({
  p,
  selected,
  onSelect,
  projectId,
}: {
  p: RuleProposal;
  selected: boolean;
  onSelect: (on: boolean) => void;
  projectId: string;
}) {
  const adopt = useAdoptRuleProposals(projectId);
  const dismiss = useDismissRuleProposal(projectId);
  const [reason, setReason] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const why = reason.trim();
  const explainId = `loosens-why-${p.id}`;
  return (
    <li className="border-b border-line py-2" data-testid={`proposal-${p.id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="checkbox"
          aria-label={`Select proposal ${p.id}`}
          aria-describedby={p.loosens ? explainId : undefined}
          disabled={p.loosens}
          checked={selected && !p.loosens}
          onChange={(e) => onSelect(e.target.checked)}
        />
        <span className={cn(MONO, "text-meta uppercase text-muted-2")}>{p.kind}</span>
        <span className="text-body font-semibold text-fg">{summary(p)}</span>
        {p.loosens && (
          <span className={cn(MONO, "rounded-[6px] border border-accent px-1 text-meta text-accent")} data-testid="loosens-flag">
            LOOSENS
          </span>
        )}
        <span className="text-small text-muted">by {p.proposer}</span>
      </div>
      {p.loosens && (
        <p id={explainId} className="mt-1 text-small text-muted">
          This loosens a rule, so it can't join a batch. Adopt it on its own with a reason.
        </p>
      )}
      <ul className="mt-1 list-none text-small text-fg-2" aria-label="Citations">
        {p.citations.map((c, i) => (
          <li key={i}>
            <Citation c={c} />
          </li>
        ))}
      </ul>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <Input
          aria-label={`Reason for proposal ${p.id}`}
          placeholder="Reason"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          className="h-7 max-w-xs"
        />
        {p.loosens && (
          <Button
            size="sm"
            disabled={!why || adopt.isPending}
            onClick={() => {
              setError(null);
              adopt.mutate({ ids: [p.id], reason: why }, { onError: (e) => setError(errorDetail(e, "Adopt refused")) });
            }}
          >
            Adopt
          </Button>
        )}
        <Button
          size="sm"
          variant="ghost"
          disabled={!why || dismiss.isPending}
          onClick={() => {
            setError(null);
            dismiss.mutate({ id: p.id, reason: why }, { onError: (e) => setError(errorDetail(e, "Dismiss refused")) });
          }}
        >
          Dismiss
        </Button>
      </div>
      {error && (
        <p role="alert" className="mt-1 text-small text-danger" data-testid={`proposal-error-${p.id}`}>
          {error}
        </p>
      )}
    </li>
  );
}

function CustomRuleRow({ r }: { r: CustomRule }) {
  return (
    <li className="py-1" data-testid={`custom-rule-${r.rule_id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={cn(MONO, "rounded-[6px] border border-control px-1 text-meta text-fg-2")}>Custom</span>
        <span className="text-body font-semibold text-fg">{r.title}</span>
        <span className="text-small text-muted">by {r.author ?? "unknown"}</span>
      </div>
      <p className="text-small text-fg-2">Why: {r.why}</p>
    </li>
  );
}

export function StageProposals({ stageId, ruleIds }: { stageId: string; ruleIds: string[] }) {
  const { activeId } = useProjectCtx();
  const { data: proposals } = useRuleProposals(activeId);
  const { data: custom } = useCustomRules(activeId);
  const adopt = useAdoptRuleProposals(activeId);
  const [picked, setPicked] = React.useState<Set<number>>(new Set());
  const [reason, setReason] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  const mine = (proposals ?? []).filter(
    (p) => p.status === "open" && (p.draft?.stage === stageId || ruleIds.includes(p.rule_id)),
  );
  const customHere = (custom ?? []).filter((r) => r.stage === stageId);
  if (!mine.length && !customHere.length) return null;

  const ids = mine.filter((p) => !p.loosens && picked.has(p.id)).map((p) => p.id);
  const why = reason.trim();
  return (
    <div className="flex-none max-h-[45%] overflow-y-auto border-b border-line px-4 py-2" data-testid="stage-proposals">
      {mine.length > 0 && (
        <>
          <h3 className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>PROPOSALS ({mine.length})</h3>
          <ul className="list-none">
            {mine.map((p) => (
              <ProposalRow
                key={p.id}
                p={p}
                projectId={activeId}
                selected={picked.has(p.id)}
                onSelect={(on) =>
                  setPicked((s) => {
                    const n = new Set(s);
                    if (on) n.add(p.id);
                    else n.delete(p.id);
                    return n;
                  })
                }
              />
            ))}
          </ul>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Input
              aria-label="Batch reason"
              placeholder="One reason for the batch"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="h-7 max-w-xs"
            />
            <Button
              size="sm"
              disabled={!ids.length || !why || adopt.isPending}
              onClick={() => {
                setError(null);
                adopt.mutate(
                  { ids, reason: why },
                  {
                    onSuccess: () => {
                      setPicked(new Set());
                      setReason("");
                    },
                    onError: (e) => setError(errorDetail(e, "Adopt refused")),
                  },
                );
              }}
            >
              Adopt selected ({ids.length})
            </Button>
          </div>
          {error && (
            <p role="alert" className="mt-1 text-small text-danger" data-testid="batch-error">
              {error}
            </p>
          )}
        </>
      )}
      {customHere.length > 0 && (
        <>
          <h3 className={cn(MONO, "mt-2 text-meta tracking-[0.07em] text-muted-2")}>CUSTOM RULES</h3>
          <ul className="list-none">
            {customHere.map((r) => (
              <CustomRuleRow key={r.rule_id} r={r} />
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
