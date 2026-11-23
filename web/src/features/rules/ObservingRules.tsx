/**
 * Observing rules and adopted guidance for one stage (GRPH-1782 / P91 S10). An observing rule is
 * drawn ADVISORY: on and measured, not enforced. It shows "observing since <date>: would have
 * held N items" (UNMEASURED when nothing was measured, never 0), a nudge once `decision_due`, the
 * would-have record, who proposed and adopted it, and a Promote button that needs a reason.
 * Adopted guidance sits under the stage as ADVISORY text with its citations.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import {
  useAdoptedProposals,
  useCustomRules,
  usePromoteRule,
  useProposalDecisions,
  useRuleGuidance,
  useRuleObservations,
  useRuleToggles,
} from "@/lib/queries";
import type { RuleObservationSummary } from "@/lib/types";

import { Citation } from "./StageProposals";

const MONO = "font-mono";
const BADGE = "rounded-[6px] border border-control px-1 text-meta text-fg-2";

function day(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 10) : "an unknown date";
}

/** UNMEASURED unless the server sent a number: a missing count is never drawn as 0. */
export function observingLabel(o: RuleObservationSummary | null | undefined): string {
  const since = `observing since ${day(o?.since)}`;
  if (!o || o.would_have == null || o.would_hold == null) return `${since}: UNMEASURED`;
  const refused = o.would_refuse != null && o.would_refuse > 0 ? `, would have refused ${o.would_refuse}` : "";
  return `${since}: would have held ${o.would_hold} item${o.would_hold === 1 ? "" : "s"}${refused}`;
}

const BRIEF_SENTENCE = "This rule is applied by the agent's brief, which records nothing.";

function shortDay(iso: string | null | undefined): string {
  if (!iso) return "adoption";
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
}

/** GRPH-1809: name who measured the would-have. "dashed" = not reported yet, "brief" = records
 * nothing; neither ever draws a count. Without `measured_by` (an older server) the plain label. */
export function measureLine(o: RuleObservationSummary | null | undefined): { kind: "plain" | "dashed" | "brief"; text: string } {
  const by = o?.measured_by;
  if (!o || !by) return { kind: "plain", text: observingLabel(o) };
  const since = shortDay(o.since);
  if (by === "brief") return { kind: "brief", text: BRIEF_SENTENCE };
  if (by === "supervisor") {
    const allowed = o.would_allow ?? 0;
    const held = o.would_hold ?? 0;
    if (!o.last_supervisor_report || (allowed + held === 0 && o.would_have == null)) {
      return { kind: "dashed", text: `Would-have not reported yet · no fleet supervisor has run since ${since}` };
    }
    const part = allowed > 0 ? `would have let through ${allowed} file${allowed === 1 ? "" : "s"}` : `would have held ${held} item${held === 1 ? "" : "s"}`;
    return { kind: "plain", text: `${part[0].toUpperCase()}${part.slice(1)} · reported by the fleet supervisor` };
  }
  // Server rule. Its would-have: held (a claim) or refused (a call), as the outcomes say.
  const held = o.would_hold ?? 0;
  const refusedN = o.would_refuse ?? 0;
  const verb = held > 0 && refusedN === 0 ? `Would have held ${held}` : held > 0
    ? `Would have held ${held}, refused ${refusedN}` : `Would have refused ${refusedN}`;
  const noun = o.checks_noun;
  // No counted denominator for this rule: the plain count, which names what it held/refused.
  if (o.checks == null || !noun) return { kind: "plain", text: observingLabel(o) };
  if (o.checks === 0 && held + refusedN === 0) {
    return { kind: "dashed", text: `Would-have not measured yet · no ${noun} checked since ${since}` };
  }
  return { kind: "plain", text: `${verb} · ${o.checks} ${noun} checked since ${since}` };
}

export function ObservingRow({
  ruleId,
  title,
  observation,
  projectId,
}: {
  ruleId: string;
  title: string;
  observation: RuleObservationSummary | null | undefined;
  projectId: string;
}) {
  const [open, setOpen] = React.useState(false);
  const [reason, setReason] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const promote = usePromoteRule(projectId);
  const { data: rows } = useRuleObservations(projectId, ruleId, true);
  const { data: adopted } = useAdoptedProposals(projectId);
  // The proposals list omits decided_by / decided_reason. The adopt event carries them (GRPH-1811).
  const { data: adoptions } = useProposalDecisions(projectId, "adopt");
  const origin = (adopted ?? []).find((p) => p.rule_id === ruleId);
  const decision = origin ? adoptions?.get(origin.id) : undefined;
  const why = reason.trim();
  const line = measureLine(observation);
  return (
    <li className="border-b border-line py-2" data-testid={`observing-${ruleId}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={cn(MONO, BADGE)} data-testid="advisory-badge">
          ADVISORY
        </span>
        <span className="text-body font-semibold text-fg">{title}</span>
      </div>
      <p
        className={cn(
          "text-small text-fg-2",
          line.kind === "dashed" && "rounded-[6px] border border-dashed border-control px-2 py-1",
          line.kind === "brief" && "rounded-[6px] border border-dotted border-danger px-2 py-1 text-danger",
        )}
        data-testid="observing-label"
        data-measure={line.kind}
      >
        {line.kind === "brief" && <span aria-hidden="true">— </span>}
        {line.text}
      </p>
      {origin && (
        <p className="text-small text-muted" data-testid="observing-origin">
          Proposed by {origin.proposer}
          {decision?.by ? `, adopted by ${decision.by}` : ""}
          {decision?.reason ? ` — “${decision.reason}”` : ""}
        </p>
      )}
      {observation?.decision_due && (
        <p role="status" className="text-small text-accent" data-testid="decision-due">
          Decision due{observation.decision_due_reason ? ` (${observation.decision_due_reason})` : ""}: promote this
          rule or remove it. It stays observing until a person decides.
        </p>
      )}
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <Input
          aria-label={`Promote reason for ${ruleId}`}
          placeholder="Reason to promote"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          className="h-7 max-w-xs"
        />
        <Button
          size="sm"
          disabled={!why || promote.isPending}
          onClick={() => {
            setError(null);
            promote.mutate({ ruleId, reason: why }, { onError: (e) => setError(errorDetail(e, "Promote refused")) });
          }}
        >
          Promote
        </Button>
        <button type="button" className="text-[11.5px] text-accent underline" onClick={() => setOpen(!open)}>
          {open ? "Hide would-have record" : `Would-have record (${rows?.length ?? 0})`}
        </button>
      </div>
      {error && (
        <p role="alert" className="mt-1 text-small text-danger" data-testid={`promote-error-${ruleId}`}>
          {error}
        </p>
      )}
      {open && (
        <ol className="mt-1 list-none text-small text-fg-2" data-testid={`would-have-${ruleId}`}>
          {(rows ?? []).map((o) => (
            <li key={o.id}>
              {o.outcome === "would_hold" ? "Would have held" : "Would have refused"}
              {o.item_id ? ` ${o.item_id}` : ""} · {day(o.at)}
            </li>
          ))}
          {!rows?.length && <li>Nothing recorded yet.</li>}
        </ol>
      )}
    </li>
  );
}

/**
 * The would-have record of a custom rule that no longer observes (GRPH-1880, D5). Promotion never
 * purges the rows, so the reason a rule was promoted stays readable from the custom rule view even
 * once get_rule_toggles stops carrying an observation summary.
 */
export function WouldHaveRecord({ ruleId, projectId }: { ruleId: string; projectId: string }) {
  const { data: rows, isError } = useRuleObservations(projectId, ruleId, true);
  return (
    <div className="flex flex-col gap-1" data-testid={`would-have-record-${ruleId}`}>
      <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>
        WOULD-HAVE RECORD{rows ? ` (${rows.length})` : ""}
      </span>
      <ol className="list-none text-small text-fg-2" data-testid={`would-have-${ruleId}`}>
        {(rows ?? []).map((o) => (
          <li key={o.id}>
            {o.outcome === "would_hold" ? "Would have held" : "Would have refused"}
            {o.item_id ? ` ${o.item_id}` : ""} · {day(o.at)}
          </li>
        ))}
        {rows && !rows.length && <li>Nothing recorded while it observed.</li>}
        {!rows && <li>{isError ? "Would-have record not read." : "Reading the would-have record…"}</li>}
      </ol>
    </div>
  );
}

export function ObservingRules({ stageId, ruleIds }: { stageId: string; ruleIds: string[] }) {
  const { activeId } = useProjectCtx();
  const { data: toggles } = useRuleToggles(activeId);
  const { data: custom } = useCustomRules(activeId);
  const { data: guidance } = useRuleGuidance(activeId);
  const here = stageId.toLowerCase();
  const sameStage = (s: unknown) => typeof s === "string" && s.toLowerCase() === here;

  const byId = new Map((toggles?.toggles ?? []).map((t) => [t.rule_id, t]));
  const seen = new Set<string>();
  const rows: { id: string; title: string; observation: RuleObservationSummary | null | undefined }[] = [];
  for (const t of toggles?.toggles ?? []) {
    if (t.state === "observing" && (sameStage(t.stage) || ruleIds.includes(t.rule_id))) {
      seen.add(t.rule_id);
      rows.push({ id: t.rule_id, title: t.title, observation: t.observation });
    }
  }
  for (const r of custom ?? []) {
    if (r.state === "observing" && sameStage(r.stage) && !seen.has(r.rule_id)) {
      rows.push({ id: r.rule_id, title: r.title, observation: byId.get(r.rule_id)?.observation });
    }
  }
  const notes = (guidance ?? []).filter((g) => g.status === "adopted" && sameStage(g.stage));
  if (!rows.length && !notes.length) return null;
  return (
    <div className="flex-none max-h-[45%] overflow-y-auto border-b border-line px-4 py-2" data-testid="stage-observing">
      {rows.length > 0 && (
        <>
          <h3 className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>OBSERVING ({rows.length})</h3>
          <ul className="list-none">
            {rows.map((r) => (
              <ObservingRow key={r.id} ruleId={r.id} title={r.title} observation={r.observation} projectId={activeId} />
            ))}
          </ul>
        </>
      )}
      {notes.length > 0 && (
        <>
          <h3 className={cn(MONO, "mt-2 text-meta tracking-[0.07em] text-muted-2")}>GUIDANCE</h3>
          <ul className="list-none">
            {notes.map((g) => (
              <li key={g.id} className="py-1" data-testid={`guidance-${g.id}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className={cn(MONO, BADGE)}>ADVISORY</span>
                  <span className="text-body font-semibold text-fg">{g.label}</span>
                </div>
                <p className="text-small text-fg-2">{g.text}</p>
                <ul className="list-none text-small text-muted" aria-label="Citations">
                  {g.citations.map((c, i) => (
                    <li key={i}>
                      <Citation c={c} />
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
