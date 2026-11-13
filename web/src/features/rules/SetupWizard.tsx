/**
 * The setup wizard (GRPH-1601): a new project sets its repo-specific rules by answering plain
 * questions, one stage at a time (Plan, Build, Review, Merge, Learn). Spec and Release are
 * skipped, and the page says why. Every question starts on "Keep the default". With no repo
 * detections yet, every question says "Nothing detected" and a person sets the value with the
 * shared editors. Answers live in this component's memory only: nothing is stored before Apply,
 * and Exit, in-app navigation or a reload discards them (after a confirm when an answer differs from its default).
 * The last step is the shared review sheet with one reason; it saves through the batch write,
 * all-or-nothing. A refusal returns to the step holding the refused rule with the server's words,
 * and every answer and the reason are kept.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { useSetRuleToggles } from "@/lib/queries";
import type { RuleFlowRule, RuleToggleBatchEntry, RulesFlow } from "@/lib/types";

import { cleanParams, useCanWriteRules } from "./LibraryRuleControls";
import { RuleReviewSheet, type Params, type RuleChange } from "./RuleReviewSheet";
import { enumFor, RuleValueEditor } from "./RuleValueEditor";

const MONO = "font-mono";

/** The rules the page just changed, so the map can highlight them after Apply. */
export const ChangedRulesContext = React.createContext<ReadonlySet<string>>(new Set());

export const WIZARD_STEPS = ["plan", "build", "review", "merge", "learn"] as const;
export const SKIPPED_STEPS: { id: string; title: string; reason: string }[] = [
  { id: "spec", title: "Spec", reason: "Skipped: nothing in Spec is repo-specific, so there is nothing to ask." },
  { id: "release", title: "Release", reason: "Skipped: Release is set when you deploy, not when you set up rules." },
];

const LEAVE_PROMPT = "Leave setup? Your answers are not saved and will be discarded.";

const isAskable = (r: RuleFlowRule) =>
  r.control === "toggle" && !r.unmeasured_reason && !r.locked && !!r.params && Object.keys(r.params.value ?? {}).length > 0;

/** One answer: absent = "Keep the default". */
type Answers = Record<string, Params>;

export function SetupWizard({
  flow,
  narrow,
  onExit,
  onApplied,
}: {
  flow: RulesFlow;
  narrow: boolean;
  onExit: () => void;
  onApplied: (ruleIds: string[]) => void;
}) {
  const { activeId } = useProjectCtx();
  const canWrite = useCanWriteRules(activeId);
  const save = useSetRuleToggles(activeId);
  const steps = WIZARD_STEPS.map((id) => flow.stages.find((s) => s.id === id)).filter((s): s is NonNullable<typeof s> => !!s);
  const [index, setIndex] = React.useState(0); // index === steps.length is the review step
  const [answers, setAnswers] = React.useState<Answers>({});
  const [reason, setReason] = React.useState("");
  const [sheetError, setSheetError] = React.useState<string | null>(null);
  const [refused, setRefused] = React.useState<Record<string, string>>({});
  const [paramErrors, setParamErrors] = React.useState<Record<string, string>>({});

  const allRules = steps.flatMap((s) => s.rules);
  const { changes, entries } = React.useMemo(() => {
    const changes: RuleChange[] = [];
    const entries: RuleToggleBatchEntry[] = [];
    for (const r of allRules) {
      const a = answers[r.id];
      if (!a) continue;
      const current = (r.params?.value as Params | null) ?? {};
      const cleaned = cleanParams(a);
      const changed = Object.keys(cleaned).filter((k) => JSON.stringify(cleaned[k]) !== JSON.stringify(current[k]));
      if (changed.length === 0) continue;
      changes.push({
        kind: "rule",
        ruleId: r.id,
        title: r.title,
        params: changed.map((k) => {
          const after = cleaned[k];
          const opt = typeof after === "string" ? enumFor(r.id, k)?.find((o) => o.value === after) : undefined;
          return { name: k, before: current[k], after, risk: opt?.risky ? `${opt.label}: ${opt.explain}` : undefined };
        }),
        sourceBefore: r.params?.source ?? r.source ?? "platform default",
        sourceAfter: "project",
      });
      entries.push({ rule_id: r.id, params: cleaned });
    }
    return { changes, entries };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [answers, flow]);

  const dirty = changes.length > 0;
  // A reload discards the answers; warn first when one differs from its default.
  React.useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  // In-app navigation unmounts the wizard and would drop the answers silently. The app runs on a
  // BrowserRouter (no useBlocker), so catch a click on any in-app link outside the wizard before
  // the router sees it, and ask first. Declining cancels the navigation; the answers stay.
  const region = React.useRef<HTMLDivElement | null>(null);
  React.useEffect(() => {
    if (!dirty) return;
    const guard = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const a = e.target instanceof Element ? e.target.closest("a[href]") : null;
      if (!(a instanceof HTMLAnchorElement) || region.current?.contains(a)) return;
      if (a.target && a.target !== "_self") return;
      if (a.origin !== window.location.origin) return;
      if (window.confirm(LEAVE_PROMPT)) return;
      e.preventDefault();
      e.stopPropagation();
    };
    document.addEventListener("click", guard, true);
    return () => document.removeEventListener("click", guard, true);
  }, [dirty]);

  function exit() {
    if (dirty && !window.confirm(LEAVE_PROMPT)) return;
    onExit();
  }

  function setAnswer(rule: RuleFlowRule, name: string, value: Params[string]) {
    setAnswers((a) => ({ ...a, [rule.id]: { ...((rule.params?.value as Params | null) ?? {}), ...a[rule.id], [name]: value } }));
    setRefused((e) => {
      const { [rule.id]: _drop, ...rest } = e;
      return rest;
    });
  }
  function keepDefault(rule: RuleFlowRule) {
    setAnswers((a) => {
      const { [rule.id]: _drop, ...rest } = a;
      return rest;
    });
  }

  async function apply() {
    setSheetError(null);
    setRefused({});
    try {
      await save.mutateAsync({ toggles: entries, reason: reason.trim() });
    } catch (err) {
      // All-or-nothing: nothing was applied. Back to the step that holds the refused rule.
      const detail = errorDetail(err, "The server refused the change.");
      const hit = entries.find((e) => detail.startsWith(`${e.rule_id}: `));
      if (hit) {
        setRefused({ [hit.rule_id]: detail.slice(hit.rule_id.length + 2) });
        const at = steps.findIndex((s) => s.rules.some((r) => r.id === hit.rule_id));
        if (at >= 0) setIndex(at);
      } else {
        setSheetError(detail);
      }
      return;
    }
    onApplied(entries.map((e) => e.rule_id));
  }

  const step = steps[index];
  const firstParamError = Object.values(paramErrors)[0];
  const atReview = index >= steps.length;
  const questions = step ? step.rules.filter(isAskable) : [];

  return (
    <div ref={region} className="absolute inset-0 z-20 overflow-y-auto bg-bg" data-testid="rules-setup-wizard" role="region" aria-label="Set up rules">
      <div className="mx-auto flex max-w-[720px] flex-col gap-4 p-5">
        <div className="flex items-center gap-2">
          <span className={cn(MONO, "text-[10px] font-semibold tracking-[0.06em] text-accent")}>SET UP RULES</span>
          <span className="flex-1" />
          <Button size="sm" variant="outline" onClick={exit} data-testid="rules-wizard-exit">
            Exit
          </Button>
        </div>
        <ol className="flex flex-wrap gap-2" aria-label="Setup steps">
          {steps.map((s, i) => (
            <li key={s.id}>
              <button
                type="button"
                aria-current={i === index ? "step" : undefined}
                onClick={() => setIndex(i)}
                className={cn("h-7 rounded-[8px] border px-2.5 text-small", i === index ? "border-accent text-accent" : "border-control text-fg-2")}
              >
                {i + 1}. {s.title}
              </button>
            </li>
          ))}
          <li>
            <button
              type="button"
              aria-current={atReview ? "step" : undefined}
              onClick={() => setIndex(steps.length)}
              className={cn("h-7 rounded-[8px] border px-2.5 text-small", atReview ? "border-accent text-accent" : "border-control text-fg-2")}
            >
              Apply
            </button>
          </li>
        </ol>
        <ul className="flex flex-col gap-0.5" data-testid="rules-wizard-skipped">
          {SKIPPED_STEPS.map((s) => (
            <li key={s.id} className="text-[11.5px] text-muted">
              {s.title}: {s.reason}
            </li>
          ))}
        </ul>

        {step && (
          <section className="flex flex-col gap-3" aria-label={`${step.title} questions`} data-testid={`rules-wizard-step-${step.id}`}>
            <h2 className="text-[16px] font-semibold">{step.title}</h2>
            {questions.length === 0 && <p className="text-body text-fg-2">Nothing to ask in {step.title}. Keep the defaults and go on.</p>}
            {questions.map((r) => {
              const custom = !!answers[r.id];
              const shown: Params = answers[r.id] ?? ((r.params?.value as Params | null) ?? {});
              return (
                <div
                  key={r.id}
                  data-testid={`rules-wizard-q-${r.id}`}
                  className="flex flex-col gap-2 rounded-[12px] border border-[#2a333a] p-3"
                  style={refused[r.id] ? { borderColor: "#ff6b6b" } : undefined}
                >
                  <span className="text-[13px] font-semibold leading-snug">{r.title}</span>
                  {r.summary && <span className="text-[12px] leading-snug text-fg-2">{r.summary}</span>}
                  <span className={cn(MONO, "text-[10.5px] text-muted")}>{r.id}</span>
                  <span className="text-[11.5px] text-muted" data-testid="rules-wizard-detected">Nothing detected</span>
                  <div className="flex gap-1.5" role="group" aria-label={`${r.title}: your answer`}>
                    <button
                      type="button"
                      aria-pressed={!custom}
                      onClick={() => keepDefault(r)}
                      className={cn("h-7 rounded-[8px] border px-2.5 text-small", !custom ? "border-accent text-accent" : "border-control text-fg-2")}
                    >
                      Keep the default
                    </button>
                    <button
                      type="button"
                      aria-pressed={custom}
                      onClick={() => setAnswers((a) => ({ ...a, [r.id]: { ...((r.params?.value as Params | null) ?? {}) } }))}
                      className={cn("h-7 rounded-[8px] border px-2.5 text-small", custom ? "border-accent text-accent" : "border-control text-fg-2")}
                    >
                      Set my own
                    </button>
                  </div>
                  {custom &&
                    Object.keys((r.params?.value as Params | null) ?? {}).map((k) => (
                      <RuleValueEditor
                        key={k}
                        ruleId={r.id}
                        name={k}
                        value={shown[k]}
                        onChange={(nv) => setAnswer(r, k, nv)}
                        onError={(m) =>
                          setParamErrors((e) => {
                            const { [`${r.id}:${k}`]: _drop, ...rest } = e;
                            return m ? { ...rest, [`${r.id}:${k}`]: m } : rest;
                          })
                        }
                      />
                    ))}
                  {refused[r.id] && (
                    <p role="alert" data-testid={`rules-wizard-refused-${r.id}`} className="text-[12px] leading-snug text-st-blocked">
                      {refused[r.id]} Nothing was applied; your answers are kept.
                    </p>
                  )}
                </div>
              );
            })}
          </section>
        )}

        {atReview && (
          <section className="flex flex-col gap-2" data-testid="rules-wizard-review-step">
            <h2 className="text-[16px] font-semibold">Review</h2>
            {changes.length === 0 ? (
              <p className="text-body text-fg-2" data-testid="rules-wizard-no-changes">
                Every answer is "Keep the default", so there is nothing to apply.
              </p>
            ) : (
              <p className="text-body text-fg-2">Check the changes, give one reason, and apply them together.</p>
            )}
          </section>
        )}

        {firstParamError && <p role="alert" className="text-[12px] text-st-blocked">{firstParamError}</p>}
        <div className="flex gap-2">
          <Button size="sm" variant="outline" disabled={index === 0} onClick={() => setIndex((i) => i - 1)} data-testid="rules-wizard-back">
            Back
          </Button>
          {!atReview ? (
            <Button size="sm" onClick={() => setIndex((i) => i + 1)} disabled={!!firstParamError} data-testid="rules-wizard-next">
              {index === steps.length - 1 ? "Review" : "Next"}
            </Button>
          ) : null}
        </div>
      </div>
      {atReview && changes.length > 0 && (
        <RuleReviewSheet
          changes={changes}
          reason={reason}
          onReason={setReason}
          saving={save.isPending}
          error={sheetError ?? (canWrite === false ? "You don't have a role that can change rules." : null)}
          narrow={narrow}
          onSave={() => void apply()}
          onBack={() => setIndex(steps.length - 1)}
        />
      )}
    </div>
  );
}
