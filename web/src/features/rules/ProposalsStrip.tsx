/**
 * One proposals strip above the stage map (GRPH-1807, P91 D4 "A person adopts"). Every proposal
 * decision lives here, not in a stage. Nothing is pre-ticked and there is no select-all. A
 * loosening cannot join a batch and has its own Adopt with its own reason. A batch is
 * all-or-nothing: on a refusal the ticks and reason stay and the server's text is shown verbatim.
 * Members see the strip read-only.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { useAdoptRuleProposals, useDismissedProposals, useDismissRuleProposal, useRuleProposals } from "@/lib/queries";
import { relTime } from "@/lib/relTime";
import type { RuleProposal } from "@/lib/types";

import { useCanWriteRules } from "./LibraryRuleControls";
import { Citation } from "./StageProposals";

const MONO = "font-mono";
export const LOOSENS_WHY = "This loosens a rule, so it can't join a batch. Adopt it on its own with a reason.";
export const DISMISS_NOTE = "Dismissing needs a reason. It is kept with the proposal; the proposer is not notified.";

/** The stage a proposal belongs to, lowercase, or "" when it names none. */
export function proposalStage(p: RuleProposal): string {
  const s = p.draft?.stage;
  return typeof s === "string" ? s.toLowerCase() : "";
}

export function proposalTitle(p: RuleProposal): string {
  const title = typeof p.draft?.title === "string" ? p.draft.title : "";
  if (title) return title;
  const params = Object.keys(p.params ?? {}).length ? JSON.stringify(p.params) : "";
  return params ? `${p.rule_id} → ${params}` : p.rule_id;
}

function Card({
  p,
  canAct,
  picked,
  onPick,
  onDismiss,
  projectId,
}: {
  p: RuleProposal;
  canAct: boolean;
  picked: boolean;
  onPick: (on: boolean) => void;
  onDismiss: () => void;
  projectId: string;
}) {
  const adopt = useAdoptRuleProposals(projectId);
  const [reason, setReason] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const why = reason.trim();
  const stage = proposalStage(p);
  const explainId = `loosens-why-${p.id}`;
  return (
    <li className="border-b border-line py-2" data-testid={`proposal-${p.id}`}>
      <div className="flex flex-wrap items-center gap-2">
        {canAct && (
          <input
            type="checkbox"
            aria-label={`Select proposal ${p.id}`}
            aria-describedby={p.loosens ? explainId : undefined}
            disabled={p.loosens}
            checked={picked && !p.loosens}
            onChange={(e) => onPick(e.target.checked)}
          />
        )}
        <span className={cn(MONO, "text-meta uppercase text-muted-2")}>{p.kind}</span>
        <span className="text-body font-semibold text-fg">{proposalTitle(p)}</span>
        <span className={cn(MONO, "text-meta text-muted-2")}>{p.rule_id}</span>
        {stage && (
          <span className={cn(MONO, "rounded-[6px] border border-control px-1 text-meta text-fg-2")} data-testid="stage-chip">
            {stage}
          </span>
        )}
        {p.loosens && (
          <span className={cn(MONO, "rounded-[6px] border border-accent px-1 text-meta text-accent")} data-testid="loosens-flag">
            LOOSENS
          </span>
        )}
        <span className="text-small text-muted" data-testid="proposer">
          proposed by {p.proposer_kind === "agent" ? `Agent ${p.proposer}` : p.proposer}
        </span>
      </div>
      {p.loosens && (
        <p id={explainId} className="mt-1 text-small text-muted">
          {LOOSENS_WHY}
        </p>
      )}
      <ul className="mt-1 list-none text-small text-fg-2" aria-label="Citations">
        {p.citations.map((c, i) => (
          <li key={i}>
            <Citation c={c} />
          </li>
        ))}
      </ul>
      {canAct && (
        <div className="mt-1 flex flex-wrap items-center gap-2">
          {p.loosens && (
            <>
              <Input
                aria-label={`Reason for proposal ${p.id}`}
                placeholder="Reason"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                className="h-7 max-w-xs"
              />
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
            </>
          )}
          <Button size="sm" variant="ghost" onClick={onDismiss}>
            Dismiss
          </Button>
        </div>
      )}
      {error && (
        <p role="alert" className="mt-1 text-small text-danger" data-testid={`proposal-error-${p.id}`}>
          {error}
        </p>
      )}
    </li>
  );
}

function DismissSheet({ p, projectId, onClose }: { p: RuleProposal; projectId: string; onClose: () => void }) {
  const dismiss = useDismissRuleProposal(projectId);
  const [reason, setReason] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const submit = () => {
    const why = reason.trim();
    if (!why) {
      setError("A reason is required to dismiss.");
      return;
    }
    setError(null);
    dismiss.mutate({ id: p.id, reason: why }, { onSuccess: onClose, onError: (e) => setError(errorDetail(e, "Dismiss refused")) });
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent data-testid="dismiss-sheet">
        <DialogHeader>
          <DialogTitle>Dismiss {proposalTitle(p)}</DialogTitle>
          <DialogDescription>{DISMISS_NOTE}</DialogDescription>
        </DialogHeader>
        <Input aria-label="Dismiss reason" placeholder="Reason" value={reason} onChange={(e) => setReason(e.target.value)} />
        {error && (
          <p role="alert" className="mt-1 text-small text-danger" data-testid="dismiss-error">
            {error}
          </p>
        )}
        <div className="mt-3 flex gap-2">
          <Button size="sm" disabled={dismiss.isPending} onClick={submit}>
            Dismiss proposal
          </Button>
          <Button size="sm" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function ProposalsStrip({ stageFilter, onClearFilter }: { stageFilter: string | null; onClearFilter: () => void }) {
  const { activeId } = useProjectCtx();
  const canWrite = useCanWriteRules(activeId);
  const canAct = canWrite === true;
  const { data: proposals } = useRuleProposals(activeId);
  const { data: dismissed } = useDismissedProposals(activeId);
  const adopt = useAdoptRuleProposals(activeId);
  const [open, setOpen] = React.useState(true);
  const [picked, setPicked] = React.useState<Set<number>>(new Set());
  const [reason, setReason] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [dismissing, setDismissing] = React.useState<RuleProposal | null>(null);
  const [showDismissed, setShowDismissed] = React.useState(false);

  // Opening the strip for a stage ("Review in proposals") expands it.
  React.useEffect(() => {
    if (stageFilter) setOpen(true);
  }, [stageFilter]);

  const allOpen = (proposals ?? []).filter((p) => p.status === "open");
  const shown = stageFilter ? allOpen.filter((p) => proposalStage(p) === stageFilter.toLowerCase()) : allOpen;
  const gone = dismissed ?? [];
  if (!allOpen.length && !gone.length) return null;

  const ids = shown.filter((p) => !p.loosens && picked.has(p.id)).map((p) => p.id);
  const why = reason.trim();
  return (
    <section aria-label="Proposals" className="flex-none border-b border-line px-4 py-2" data-testid="proposals-strip">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}
          data-testid="proposals-toggle"
        >
          Proposals ({allOpen.length} open)
        </button>
        {stageFilter && (
          <button type="button" onClick={onClearFilter} className="text-small text-accent underline" data-testid="proposals-filter">
            Showing {stageFilter} only · Show all
          </button>
        )}
      </div>
      {open && (
        <div className="max-h-[40vh] overflow-y-auto">
          <ul className="list-none">
            {shown.map((p) => (
              <Card
                key={p.id}
                p={p}
                projectId={activeId}
                canAct={canAct}
                picked={picked.has(p.id)}
                onPick={(on) =>
                  setPicked((s) => {
                    const n = new Set(s);
                    if (on) n.add(p.id);
                    else n.delete(p.id);
                    return n;
                  })
                }
                onDismiss={() => setDismissing(p)}
              />
            ))}
          </ul>
          {gone.length > 0 && (
            <div className="mt-1">
              <button
                type="button"
                aria-expanded={showDismissed}
                onClick={() => setShowDismissed(!showDismissed)}
                className="text-small text-accent underline"
                data-testid="dismissed-toggle"
              >
                Dismissed ({gone.length})
              </button>
              {showDismissed && (
                <ul className="list-none" data-testid="dismissed-list">
                  {gone.map((p) => (
                    <li key={p.id} className="py-1 text-small text-fg-2" data-testid={`dismissed-${p.id}`}>
                      {proposalTitle(p)} — dismissed by {p.decided_by ?? "unknown"}
                      {p.decided_at ? ` ${relTime(p.decided_at) ?? ""}` : ""}
                      {p.decided_reason ? ` — “${p.decided_reason}”` : ""}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
      {canAct && ids.length > 0 && (
        <div className="sticky bottom-0 mt-2 flex flex-wrap items-center gap-2 bg-surface-2 py-2" data-testid="batch-bar">
          <Input
            aria-label="Batch reason"
            placeholder="One reason for the batch"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className="h-7 max-w-xs"
          />
          <Button
            size="sm"
            disabled={!why || adopt.isPending}
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
            Adopt {ids.length} proposals
          </Button>
        </div>
      )}
      {error && (
        <p role="alert" className="mt-1 text-small text-danger" data-testid="batch-error">
          {error}
        </p>
      )}
      {dismissing && <DismissSheet p={dismissing} projectId={activeId} onClose={() => setDismissing(null)} />}
    </section>
  );
}
