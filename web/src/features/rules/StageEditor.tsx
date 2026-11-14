/**
 * The stage editor (GRPH-1599): "Edit <Stage> rules" opens ONE draft that collects every change
 * in the stage; "Review changes" opens the review sheet, which asks for one reason and saves the
 * lot through the batch write (`PUT /rules/toggles`, all-or-nothing). A refusal reopens the draft
 * with the server's words on the rule it names; nothing was saved, and the rest of the draft and
 * the reason are kept. Gitops settings are labelled as such and save through `PATCH
 * /projects/{id}/gitops`. Fixed and earned rules are text with their reason, never inputs.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { useGitops, useRuleToggles, useSetRuleToggles, useUpdateGitops } from "@/lib/queries";
import type { GitopsPatch, RuleFlowRule, RuleStage, RuleToggleBatchEntry } from "@/lib/types";

import { stageRules } from "./levels";
import { cleanParams, useCanWriteRules } from "./LibraryRuleControls";
import {
  currentValue,
  GITOPS_RULE_FIELDS,
  readOnlyCopy,
  SELECTS,
  toPatch,
  type FieldKey,
} from "./RuleEditors";
import { RuleReviewSheet, type GitopsChange, type Params, type RuleChange, type StageChange } from "./RuleReviewSheet";
import { ReadOnlyReason } from "./RuleSource";
import { enumFor, RuleValueEditor } from "./RuleValueEditor";

const MONO = "font-mono";

/** Rules that are never settings. The reason is shown in place of an input. */
export const FIXED_RULE_REASONS: Record<string, string> = {
  "review.not_own_work": "Fixed: a builder can't review its own work. No setting changes this.",
  "gate.approval_earned_by_grill": "Earned: approval comes from finishing the grill. No setting grants it.",
};

const isEditableLibrary = (r: RuleFlowRule) => r.control === "toggle" && !r.unmeasured_reason && !r.locked;
const isGitops = (r: RuleFlowRule) => r.id in GITOPS_RULE_FIELDS;

/** Whether a stage has anything a person can change in a draft. */
export function stageIsEditable(stage: RuleStage): boolean {
  return stageRules(stage).some((r) => isEditableLibrary(r) || isGitops(r));
}

type RuleDraft = { on?: boolean; params?: Params };

export function StageEditor({ stage, narrow, onClose }: { stage: RuleStage; narrow: boolean; onClose: () => void }) {
  const { activeId } = useProjectCtx();
  const canWrite = useCanWriteRules(activeId);
  const { data: gitops } = useGitops(activeId);
  const { data: toggles } = useRuleToggles(activeId);
  const saveRules = useSetRuleToggles(activeId);
  const saveGitops = useUpdateGitops(activeId);

  const [draft, setDraft] = React.useState<Record<string, RuleDraft>>({});
  const [gitopsDraft, setGitopsDraft] = React.useState<Record<string, string>>({});
  const [paramErrors, setParamErrors] = React.useState<Record<string, string>>({});
  const [ruleErrors, setRuleErrors] = React.useState<Record<string, string>>({});
  const [gitopsErrors, setGitopsErrors] = React.useState<Record<string, string>>({});
  const [reason, setReason] = React.useState("");
  const [sheetOpen, setSheetOpen] = React.useState(false);
  const [sheetError, setSheetError] = React.useState<string | null>(null);

  const gitopsEditable = !!gitops && gitops.control.writable && gitops.control.state === "local";
  const rules = stageRules(stage);

  // The changes, as they will be shown and as they will be sent.
  const { changes, entries, patch } = React.useMemo(() => {
    const changes: StageChange[] = [];
    const entries: RuleToggleBatchEntry[] = [];
    for (const r of rules) {
      const d = draft[r.id];
      if (!d || !isEditableLibrary(r)) continue;
      const current = (r.params?.value as Params | null) ?? {};
      const on = r.value === true;
      const change: RuleChange = {
        kind: "rule",
        ruleId: r.id,
        title: r.title,
        params: [],
        sourceBefore: r.params?.source ?? r.source ?? "platform default",
        sourceAfter: "project",
      };
      const entry: RuleToggleBatchEntry = { rule_id: r.id };
      if (d.on !== undefined && d.on !== on) {
        change.on = { before: on, after: d.on };
        entry.on = d.on;
      }
      if (d.params) {
        const cleaned = cleanParams(d.params);
        const changed = Object.keys(cleaned).filter((k) => JSON.stringify(cleaned[k]) !== JSON.stringify(current[k]));
        if (changed.length > 0) {
          entry.params = cleaned;
          for (const k of changed) {
            const after = cleaned[k];
            const opt = typeof after === "string" ? enumFor(r.id, k)?.find((o) => o.value === after) : undefined;
            change.params.push({ name: k, before: current[k], after, risk: opt?.risky ? `${opt.label}: ${opt.explain}` : undefined });
          }
        }
      }
      if (change.on || change.params.length > 0) {
        changes.push(change);
        entries.push(entry);
      }
    }
    const patch: GitopsPatch = {};
    for (const r of rules) {
      const field = GITOPS_RULE_FIELDS[r.id];
      if (!field || !gitops || gitopsDraft[field] === undefined) continue;
      const before = currentValue(gitops, field);
      if (gitopsDraft[field] === before) continue;
      Object.assign(patch, toPatch(field, gitopsDraft[field]));
      const g: GitopsChange = { kind: "gitops", field, title: r.title, before, after: gitopsDraft[field] };
      changes.push(g);
    }
    return { changes, entries, patch };
  }, [rules, draft, gitopsDraft, gitops]);

  const firstParamError = Object.values(paramErrors)[0];
  const hasRuleErrors = Object.keys(ruleErrors).length + Object.keys(gitopsErrors).length > 0;
  const gitopsRefusal = Object.values(gitopsErrors)[0];

  function setParam(rule: RuleFlowRule, name: string, value: Params[string]) {
    setDraft((d) => {
      const base = d[rule.id]?.params ?? { ...((rule.params?.value as Params | null) ?? {}) };
      return { ...d, [rule.id]: { ...d[rule.id], params: { ...base, [name]: value } } };
    });
    setRuleErrors((e) => {
      const { [rule.id]: _drop, ...rest } = e;
      return rest;
    });
  }

  /** The server names the rule it refused as `<rule_id>: <words>`; the words go on that rule. */
  function place(err: unknown) {
    const detail = errorDetail(err, "The server refused the change.");
    const hit = entries.find((e) => detail.startsWith(`${e.rule_id}: `));
    if (hit) {
      setRuleErrors({ [hit.rule_id]: detail.slice(hit.rule_id.length + 2) });
      setSheetError(null);
    } else {
      setRuleErrors({});
      setSheetError(detail);
    }
  }

  async function save() {
    setSheetError(null);
    setRuleErrors({});
    setGitopsErrors({});
    const hasPatch = Object.keys(patch).length > 0;
    // One outcome (GRPH-1632): the rule batch and the gitops patch go in one server call, so a
    // refusal on either leaves both unwritten. A gitops-only change is its own single write.
    try {
      if (entries.length > 0) {
        await saveRules.mutateAsync({ toggles: entries, reason: reason.trim(), ...(hasPatch ? { gitops: patch } : {}) });
      } else if (hasPatch) {
        await saveGitops.mutateAsync(patch);
      }
    } catch (err) {
      const detail = errorDetail(err, "");
      const ruleHit = entries.some((e) => detail.startsWith(`${e.rule_id}: `));
      if (hasPatch && !ruleHit) {
        const field = Object.keys(patch)[0];
        setGitopsErrors({ [field]: errorDetail(err, "Could not save gitops.") });
      } else {
        place(err);
      }
      setSheetOpen(false);
      return;
    }
    setDraft({});
    setSheetOpen(false);
    onClose();
  }

  const saving = saveRules.isPending || saveGitops.isPending;

  return (
    <div className="flex flex-col gap-2" data-testid="rules-stage-editor">
      <div className="flex flex-wrap items-center gap-2">
        <span className={cn(MONO, "text-[10px] font-semibold tracking-[0.06em] text-accent")}>EDITING {stage.title.toUpperCase()} · DRAFT</span>
        <span className="flex-1" />
        <Button size="sm" variant="outline" disabled={saving} onClick={onClose} data-testid="rules-draft-discard">
          {changes.length ? "Discard draft" : "Close"}
        </Button>
        <Button
          size="sm"
          disabled={changes.length === 0 || !!firstParamError || canWrite === false}
          onClick={() => setSheetOpen(true)}
          data-testid="rules-review-open"
        >
          Review changes{changes.length ? ` · ${changes.length}` : ""}
        </Button>
      </div>
      {firstParamError && <p role="alert" className="text-[12px] text-st-blocked">{firstParamError}</p>}
      {hasRuleErrors ? (
        <p className="text-[12px] text-fg-2" data-testid="rules-draft-refused">
          Nothing was saved. Fix the {gitopsRefusal ? "gitops setting" : "rule"} below and review again; the rest of your draft is kept.
        </p>
      ) : null}

      {rules.map((r) => {
        const d = draft[r.id];
        const params: Params = (r.params?.value as Params | null) ?? {};
        const shownParams: Params = d?.params ?? params;
        const on = d?.on ?? r.value === true;
        const field = GITOPS_RULE_FIELDS[r.id];
        const fixed = FIXED_RULE_REASONS[r.id];
        return (
          <div
            key={r.id}
            data-testid={`rules-edit-${r.id}`}
            className="flex flex-col gap-1.5 rounded-[12px] border border-[#2a333a] p-3"
            style={ruleErrors[r.id] ? { borderColor: "#ff6b6b" } : undefined}
          >
            <span className="text-[13px] font-semibold leading-snug">{r.title}</span>
            <span className={cn(MONO, "break-all text-[10.5px] text-fg-2")}>{r.id}</span>
            {fixed ? (
              <ReadOnlyReason>{fixed}</ReadOnlyReason>
            ) : field ? (
              <GitopsField
                rule={r}
                field={field}
                editable={gitopsEditable}
                readOnlyText={gitops && !gitopsEditable ? gitopsReadOnly(gitops) : "Loading the gitops settings…"}
                value={gitops ? gitopsDraft[field] ?? currentValue(gitops, field) : ""}
                onChange={(v) => setGitopsDraft((g) => ({ ...g, [field]: v }))}
                error={gitopsErrors[field]}
              />
            ) : isEditableLibrary(r) ? (
              <>
                <button
                  type="button"
                  role="switch"
                  aria-checked={on}
                  aria-label={`${r.title}: ${on ? "on" : "off"}`}
                  onClick={() => setDraft((x) => ({ ...x, [r.id]: { ...x[r.id], on: !on } }))}
                  className={cn(
                    "flex h-[22px] w-[40px] items-center rounded-full border px-[2px] transition-colors",
                    on ? "justify-end border-accent bg-accent/20" : "justify-start border-control bg-surface-2",
                  )}
                >
                  <span className={cn("h-4 w-4 rounded-full", on ? "bg-accent" : "bg-muted")} />
                </button>
                {Object.entries(params).map(([k, v]) => (
                  <RuleValueEditor
                    key={k}
                    ruleId={r.id}
                    name={k}
                    value={shownParams[k] ?? v}
                    effectivePredicates={toggles?.toggles.find((t) => t.rule_id === r.id)?.effective_predicates}
                    onChange={(nv) => setParam(r, k, nv)}
                    onError={(m) =>
                      setParamErrors((e) => {
                        const { [`${r.id}:${k}`]: _drop, ...rest } = e;
                        return m ? { ...rest, [`${r.id}:${k}`]: m } : rest;
                      })
                    }
                  />
                ))}
              </>
            ) : r.locked ? (
              <ReadOnlyReason>Locked: the org set this for every project; change it on the org console.</ReadOnlyReason>
            ) : r.unmeasured_reason ? (
              <ReadOnlyReason>Unmeasured: {r.unmeasured_reason}</ReadOnlyReason>
            ) : (
              <ReadOnlyReason>Not a setting on this page: it holds by how Graphban works.</ReadOnlyReason>
            )}
            {ruleErrors[r.id] && (
              <p role="alert" data-testid={`rules-edit-error-${r.id}`} className="text-[12px] leading-snug text-st-blocked">
                {ruleErrors[r.id]}
              </p>
            )}
          </div>
        );
      })}

      {sheetOpen && (
        <RuleReviewSheet
          changes={changes}
          reason={reason}
          onReason={setReason}
          saving={saving}
          error={sheetError}
          narrow={narrow}
          onSave={() => void save()}
          onBack={() => setSheetOpen(false)}
        />
      )}
    </div>
  );
}

function gitopsReadOnly(view: NonNullable<ReturnType<typeof useGitops>["data"]>): string {
  const { state } = view.control;
  if (state === "linked_set" || state === "linked_unset" || state === "linked_unreachable") {
    return "These gitops settings come from your org; change them on the org console.";
  }
  return readOnlyCopy(view);
}

function GitopsField({
  rule,
  field,
  editable,
  readOnlyText,
  value,
  onChange,
  error,
}: {
  rule: RuleFlowRule;
  field: FieldKey;
  editable: boolean;
  readOnlyText: string;
  value: string;
  onChange: (v: string) => void;
  error?: string;
}) {
  const options = SELECTS[field];
  return (
    <div className="flex flex-col gap-1" data-testid={`rules-edit-gitops-${field}`}>
      <span className={cn(MONO, "text-[10px] tracking-[0.06em] text-muted-2")}>GITOPS SETTING</span>
      {!editable || !rule.used ? (
        <ReadOnlyReason>{rule.used ? readOnlyText : "Not used by this project's model."}</ReadOnlyReason>
      ) : field === "no_push_to_base" ? (
        <div className="flex gap-1.5" role="group" aria-label={rule.title}>
          {(["true", "false"] as const).map((v) => (
            <button
              key={v}
              type="button"
              aria-pressed={value === v}
              onClick={() => onChange(v)}
              className={cn("h-7 rounded-[8px] border px-2.5 text-small", value === v ? "border-accent text-accent" : "border-control text-fg-2")}
            >
              {v === "true" ? "Yes" : "No"}
            </button>
          ))}
        </div>
      ) : options ? (
        <select
          aria-label={rule.title}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="h-9 w-full rounded-[9px] border border-control bg-surface-2 px-3 text-[13px] text-fg"
        >
          {options.map(([v, label]) => (
            <option key={v} value={v}>{label}</option>
          ))}
        </select>
      ) : (
        <Input aria-label={rule.title} value={value} onChange={(e) => onChange(e.target.value)} />
      )}
      {error && (
        <p role="alert" data-testid={`rules-edit-gitops-error-${field}`} className="text-[12px] leading-snug text-st-blocked">
          {error}
        </p>
      )}
    </div>
  );
}
