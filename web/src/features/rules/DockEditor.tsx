/**
 * The docked rule editor (GRPH-1718, the owner's layout C). A stage opens as an editor under the
 * map: the stage's rules in a list, the current rule's form, and its resolution chain, measured
 * effect and history with the save area pinned under them. It replaces the full-width stage view
 * (GRPH-1659) and the stage editor's draft-for-the-whole-stage: one rule is edited at a time, and
 * saving goes through the all-or-nothing batch write (`PUT /rules/toggles`) with that rule's
 * whole params object. Built-in rules open read-only.
 */
import * as React from "react";

import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { useGitops, useRuleEffect, useRuleHistory, useRuleToggles, useSetRuleToggles, useUpdateGitops } from "@/lib/queries";
import type { GitopsPatch, RuleFlowRule, RuleMeasured, RuleStage, RuleToggle, RuleToggleBatchEntry } from "@/lib/types";

import { LEVELS, ruleLevel, stageLevel, stageRules, toggleSourceLabel, weakestFirst } from "./levels";
import { cleanParams, FOUND_BUG_DEFAULT, FOUND_BUG_RULE, MemoryWriteModeControl, useCanWriteRules } from "./LibraryRuleControls";
import { BudgetCapEditor, currentValue, GITOPS_RULE_FIELDS, toPatch, useOrgName } from "./RuleEditors";
import { relTime } from "@/lib/relTime";
import { showValue, type Params } from "./RuleReviewSheet";
import { inheritsFor, ReadOnlyReason, SourceChip } from "./RuleSource";
import { enumFor, RuleValueEditor } from "./RuleValueEditor";
import { FIXED_RULE_REASONS, GitopsField, gitopsReadOnly } from "./StageEditor";

const MONO = "font-mono";
export const MIN_REASON = 8;

type Group = "builtin" | "project" | "library";
const GROUPS: { id: Group; label: string }[] = [
  { id: "builtin", label: "BUILT-IN RULES" },
  { id: "project", label: "PROJECT SETTINGS" },
  { id: "library", label: "LIBRARY RULES" },
];

const isLibrary = (r: RuleFlowRule) => r.control === "toggle";
const isEditableLibrary = (r: RuleFlowRule) => isLibrary(r) && !r.unmeasured_reason && !r.locked;

export function groupOf(r: RuleFlowRule): Group {
  if (isLibrary(r)) return "library";
  if (r.id in GITOPS_RULE_FIELDS || r.control === "select" || r.id === "plan.budget_cap") return "project";
  return "builtin";
}

function stateWord(r: RuleFlowRule): string {
  if (groupOf(r) === "builtin") return "BUILT IN";
  if (!r.used) return "NOT USED";
  return r.params?.source === "org" || r.source === "org" ? "ON · ORG" : "ON";
}

type Draft = { on?: boolean; params?: Params; resetOn?: boolean; resetParams?: boolean; gitops?: string };

/**
 * One line of help per parameter. The primitive contract (`rule_pack.PRIMITIVES`) carries only a
 * param's name and type, so the words live here; a rule-specific entry (`<rule>:<param>`) wins
 * over the param name. A param with neither says so rather than showing nothing.
 */
const PARAM_HELP: Record<string, string> = {
  min_effort: "Items at or above this effort need a sabotage receipt before sign-off.",
  min_tests_failed: "How many tests the sabotage must fail for the receipt to count.",
  property: "What the reviewer must differ from the builder on.",
  path: "The file the evidence must name.",
  engine: "The test engine the evidence must come from.",
  mode: "Which behaviour this rule applies; each option says what it does.",
  engines: "Database engines the agent must run, when the mode is agent_full.",
  paths: "Path globs, one per entry.",
  predicates: "Attestations an item needs before it can be done. suite_green is the floor and cannot be removed.",
  frontend: "Globs that make a change a frontend change.",
  migration: "Globs that make a change a migration.",
  mcp_tool: "Globs that make a change an MCP tool change.",
  docs: "Globs that make a change a docs change.",
  kinds: "Evidence kinds an item must carry.",
  "build.generated_files:paths": "Generated files a builder may change without declaring them as touchpoints.",
};

export function paramHelp(ruleId: string, name: string): string {
  return PARAM_HELP[`${ruleId}:${name}`] ?? PARAM_HELP[name] ?? "No help is written for this parameter yet.";
}

/** A measured number, or the reason there isn't one: never a bare 0 for a reading that was not taken. */
function measured(m: RuleMeasured | undefined): string {
  if (!m || m.status === "unavailable") return `unavailable${m?.reason ? ` — ${m.reason}` : ""}`;
  if (m.status === "insufficient" || m.value === null) return `not enough data${m.reason ? ` — ${m.reason}` : ""}`;
  return String(m.value);
}

export function DockEditor({
  stage,
  version,
  model,
  focusRule,
  prefill,
  onClose,
  onDirty,
  detail,
}: {
  stage: RuleStage;
  version: string;
  model: string | null;
  focusRule: string | null;
  prefill: { rule: string; value: string } | null;
  onClose: () => void;
  /** Told whenever the draft turns dirty or clean, so a stage switch can ask first. */
  onDirty?: (dirty: boolean) => void;
  /** Read-only enforcement detail for a rule the form has no inputs for (enforcer, last result). */
  detail?: (rule: RuleFlowRule) => React.ReactNode;
}) {
  const { activeId } = useProjectCtx();
  const canWrite = useCanWriteRules(activeId) !== false;
  const { data: gitops } = useGitops(activeId);
  const { data: toggles } = useRuleToggles(activeId);
  const saveRules = useSetRuleToggles(activeId);
  const saveGitops = useUpdateGitops(activeId);
  const orgName = useOrgName(gitops);
  const rules = React.useMemo(() => weakestFirst(stageRules(stage)), [stage]);

  const initial =
    prefill && rules.some((r) => r.id === prefill.rule)
      ? prefill.rule
      : focusRule && rules.some((r) => r.id === focusRule)
        ? focusRule
        : rules[0]?.id ?? null;
  const [ruleId, setRuleId] = React.useState<string | null>(initial);
  const [draft, setDraft] = React.useState<Draft>(() => {
    const f = prefill ? GITOPS_RULE_FIELDS[prefill.rule] : undefined;
    return f && prefill && prefill.rule === initial ? { gitops: prefill.value } : {};
  });
  const [reason, setReason] = React.useState("");
  const [paramErrors, setParamErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<string | null>(null);
  const [saved, setSaved] = React.useState<string | null>(null);
  const [leaveTo, setLeaveTo] = React.useState<string | null>(null);
  const [confirmed, setConfirmed] = React.useState(false);
  // Editors keep their own text while typing; a new epoch remounts them on the saved values (Discard).
  const [epoch, setEpoch] = React.useState(0);

  // A selection from outside (a refusal link) follows the focus.
  React.useEffect(() => {
    if (focusRule && rules.some((r) => r.id === focusRule)) setRuleId(focusRule);
  }, [focusRule, rules]);

  const rule = rules.find((r) => r.id === ruleId) ?? null;
  const t: RuleToggle | undefined = toggles?.toggles.find((x) => x.rule_id === rule?.id);
  const effect = useRuleEffect(activeId, rule && isLibrary(rule) ? rule.id : null);
  const effectRule = effect.data?.rules.find((x) => x.rule_id === rule?.id);

  const field = rule ? GITOPS_RULE_FIELDS[rule.id] : undefined;
  const gitopsEditable = !!gitops && gitops.control.writable && gitops.control.state === "local";
  const editableLib = !!rule && isEditableLibrary(rule);
  const editable = canWrite && (editableLib || (!!field && gitopsEditable && !!rule?.used));

  // The diff: one line per changed key, before → after; and the batch entry that carries it.
  const { lines, entry, patch, risks } = React.useMemo(() => {
    const lines: string[] = [];
    const risks: string[] = [];
    let entry: RuleToggleBatchEntry | null = null;
    const patch: GitopsPatch = {};
    if (!rule) return { lines, entry, patch, risks };
    if (editableLib) {
      const e: RuleToggleBatchEntry = { rule_id: rule.id };
      const current = (rule.params?.value as Params | null) ?? {};
      const on = rule.value === true;
      if (draft.resetOn && t?.on.source === "project") {
        e.on = null;
        lines.push(`on/off: ${on ? "ON" : "OFF"} → inherited (${inheritsFor(t.inherits?.on, orgName)})`);
      } else if (draft.on !== undefined && draft.on !== on) {
        e.on = draft.on;
        lines.push(`on/off: ${on ? "ON" : "OFF"} → ${draft.on ? "ON" : "OFF"}`);
      }
      if (draft.resetParams && t?.params.source === "project") {
        e.params = null;
        lines.push(`params: project values → inherited (${inheritsFor(t.inherits?.params, orgName)})`);
      } else if (draft.params) {
        const cleaned = cleanParams(draft.params);
        const changed = Object.keys(cleaned).filter((k) => JSON.stringify(cleaned[k]) !== JSON.stringify(current[k]));
        if (changed.length > 0) {
          e.params = cleaned; // the whole object: the write replaces params, it does not merge
          for (const k of changed) {
            lines.push(`${k}: ${showValue(current[k])} → ${showValue(cleaned[k])}`);
            const after = cleaned[k];
            const opt = typeof after === "string" ? enumFor(rule.id, k)?.find((o) => o.value === after) : undefined;
            if (opt?.risky) risks.push(`${opt.label}: ${opt.explain}`);
          }
        }
      }
      if (e.on !== undefined || e.params !== undefined) entry = e;
    } else if (field && gitops && draft.gitops !== undefined) {
      const before = currentValue(gitops, field);
      if (draft.gitops !== before) {
        Object.assign(patch, toPatch(field, draft.gitops));
        lines.push(`${field}: ${showValue(before)} → ${showValue(draft.gitops)}`);
      }
    }
    return { lines, entry, patch, risks };
  }, [rule, draft, t, gitops, orgName, editableLib, field]);

  const dirty = lines.length > 0;
  React.useEffect(() => {
    onDirty?.(dirty);
  }, [dirty, onDirty]);
  // Unmounting (a stage switch, closing the dock) leaves nothing dirty behind.
  React.useEffect(() => () => onDirty?.(false), [onDirty]);
  const hasPatch = Object.keys(patch).length > 0;
  const firstParamError = Object.values(paramErrors)[0];
  const reasonOk = reason.trim().length >= MIN_REASON;
  const needsReason = !!entry; // a gitops-only save carries no audit reason
  const canSave = dirty && !firstParamError && editable && (!needsReason || reasonOk) && (risks.length === 0 || confirmed);
  const saving = saveRules.isPending || saveGitops.isPending;

  function reset() {
    setDraft({});
    setReason("");
    setParamErrors({});
    setError(null);
    setConfirmed(false);
    setEpoch((n) => n + 1);
  }

  function select(id: string) {
    if (id === ruleId) return;
    if (dirty) {
      setLeaveTo(id);
      return;
    }
    reset();
    setSaved(null);
    setRuleId(id);
  }

  // Esc closes the dock back to the map, unless it is a field's own Escape.
  const onKeyDown = (e: React.KeyboardEvent) => {
    const el = e.target as HTMLElement;
    if (e.key === "Escape" && !/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) {
      e.stopPropagation();
      onClose();
    }
  };

  async function save() {
    setError(null);
    try {
      if (entry) await saveRules.mutateAsync({ toggles: [entry], reason: reason.trim(), ...(hasPatch ? { gitops: patch } : {}) });
      else if (hasPatch) await saveGitops.mutateAsync(patch);
    } catch (err) {
      const detail = errorDetail(err, "The server refused the change.");
      setError(entry && detail.startsWith(`${entry.rule_id}: `) ? detail.slice(entry.rule_id.length + 2) : detail);
      return;
    }
    reset();
    setSaved("Saved by you · just now · in force on the next sign-off");
  }

  if (!rule) {
    return (
      <section aria-label={`${stage.title} rule editor`} data-testid="rules-dock-editor" className="flex min-h-0 flex-1 flex-col p-4">
        <p className="text-body text-muted" data-testid="rules-stage-empty">
          No rules registered for this stage yet. That isn't the same as nothing applying.
        </p>
        <FoundBugAbsent stage={stage} rules={rules} />
      </section>
    );
  }

  const savedParams: Params = (rule.params?.value as Params | null) ?? {};
  const params: Params = draft.params ?? savedParams;
  const level = ruleLevel(rule);
  const builtin = groupOf(rule) === "builtin";
  const winner = t
    ? t.on.source === "project" || t.params.source === "project"
      ? "project"
      : t.on.source === "org" || t.params.source === "org"
        ? "org"
        : "platform"
    : "platform";
  const fixed = FIXED_RULE_REASONS[rule.id];
  const onNow = draft.on ?? rule.value === true;
  const segment = draft.resetOn ? "inherit" : onNow ? "on" : "off";

  return (
    <section
      role="region"
      aria-label={`${stage.title} rule editor`}
      data-testid="rules-dock-editor"
      onKeyDown={onKeyDown}
      className="flex min-h-0 flex-1 flex-col overflow-y-auto bg-bg min-[900px]:grid min-[900px]:grid-cols-[minmax(200px,260px)_minmax(0,1fr)_minmax(240px,320px)] min-[900px]:overflow-hidden"
    >
      {/* 1 · the stage's rules */}
      <nav
        aria-label={`${stage.title} rules`}
        data-testid="rules-dock-list"
        className="flex flex-col gap-3 overflow-y-auto border-b border-line p-3 min-[900px]:border-b-0 min-[900px]:border-r"
      >
        {GROUPS.map((g) => {
          const rs = rules.filter((r) => groupOf(r) === g.id);
          if (rs.length === 0) return null;
          return (
            <div key={g.id} className="flex flex-col gap-1" data-testid={`rules-dock-group-${g.id}`}>
              <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>
                {g.label} · {rs.length}
              </span>
              {rs.map((r) => {
                const on = r.id === rule.id;
                const meta = LEVELS[ruleLevel(r)];
                return (
                  <button
                    key={r.id}
                    type="button"
                    aria-current={on ? "true" : undefined}
                    data-testid={`rules-dock-row-${r.id}`}
                    data-level={ruleLevel(r)}
                    onClick={() => select(r.id)}
                    className={cn("flex flex-col gap-0.5 rounded-[8px] px-2 py-1.5 text-left hover:bg-surface-2", on && "bg-surface-2")}
                    style={{ boxShadow: on ? "inset 3px 0 0 #c6f24e" : undefined }}
                  >
                    <span className="text-body font-semibold leading-snug text-fg">{r.title}</span>
                    <span className={cn(MONO, "text-meta tracking-[0.05em]")} style={{ color: meta.color }}>
                      {stateWord(r)}
                    </span>
                  </button>
                );
              })}
            </div>
          );
        })}
        <FoundBugAbsent stage={stage} rules={rules} />
      </nav>

      {/* 2 · the rule's form */}
      <div data-testid="rules-dock-form" className="flex min-w-0 flex-col gap-3 overflow-y-auto p-4">
        <div className="flex flex-col gap-1">
          <span className="text-lead font-semibold leading-tight">{rule.title}</span>
          <span className={cn(MONO, "break-all text-small text-fg-2")}>{rule.id}</span>
          <span className="text-body leading-normal text-muted">{rule.summary}</span>
        </div>
        {stageLevel(stage) === "not_used" && (
          <p className="text-small leading-snug text-muted" data-testid="rules-stage-not-used">
            Every rule on this stage is not used by this project's model{model ? `: ${model}` : ""}. They stay drawn so you can see what would apply.
          </p>
        )}
        {saved && (
          <p role="status" data-testid="rules-dock-saved" className="text-body text-fg-2">
            {saved}
          </p>
        )}
        {rule.unmeasured_reason && <ReadOnlyReason>Unmeasured: {rule.unmeasured_reason}</ReadOnlyReason>}
        {rule.locked && <ReadOnlyReason>Locked: the org set this for every project; change it on the org console.</ReadOnlyReason>}
        {fixed && <ReadOnlyReason>{fixed}</ReadOnlyReason>}

        <div role="group" aria-label="State" className="flex gap-1.5" data-testid="rules-dock-state">
          {(["on", "off", "inherit"] as const).map((s) => (
            <button
              key={s}
              type="button"
              disabled={!editable || !editableLib || (s === "inherit" && t?.on.source !== "project")}
              aria-pressed={segment === s}
              data-testid={`rules-dock-state-${s}`}
              onClick={() =>
                setDraft((d) => (s === "inherit" ? { ...d, resetOn: true, on: undefined } : { ...d, resetOn: false, on: s === "on" }))
              }
              className={cn(
                "h-7 rounded-[8px] border px-3 text-small capitalize disabled:opacity-50",
                segment === s ? "border-accent text-accent" : "border-control text-fg-2",
              )}
            >
              {s}
            </button>
          ))}
        </div>

        {builtin && <ReadOnlyReason>Built in: this holds by how Graphban works, so every control here is read-only.</ReadOnlyReason>}

        {editableLib && Object.keys(savedParams).length > 0 && (
          <div className="flex flex-col divide-y divide-line rounded-[8px] border border-[#2a333a]" data-testid="rules-dock-params">
            {Object.entries(savedParams).map(([k, v]) => {
              const shown = params[k] ?? v;
              const changed = JSON.stringify(shown) !== JSON.stringify(v);
              return (
                <div key={k} data-testid={`rules-dock-param-${k}`} className="flex flex-col gap-1.5 p-2.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={cn(MONO, "text-body font-semibold")}>{k}</span>
                    <SourceChip source={t?.params.source} locked={rule.locked ?? false} testId={`rules-dock-source-${k}`} />
                    {changed && <span className={cn(MONO, "text-small text-muted")}>was {showValue(v)}</span>}
                    {t?.params.source === "project" && !draft.resetParams && (
                      <button
                        type="button"
                        disabled={!editable}
                        data-testid={`rules-dock-reset-${k}`}
                        onClick={() => setDraft((d) => ({ ...d, resetParams: true, params: undefined }))}
                        className="text-small text-accent underline disabled:opacity-50"
                      >
                        Reset to {inheritsFor(t.inherits?.params, orgName)}
                      </button>
                    )}
                  </div>
                  <span className="text-small leading-snug text-muted" data-testid={`rules-dock-help-${k}`}>
                    {paramHelp(rule.id, k)}
                  </span>
                  <RuleValueEditor
                    key={epoch}
                    ruleId={rule.id}
                    name={k}
                    value={shown}
                    effectivePredicates={t?.effective_predicates}
                    onChange={(nv) => setDraft((d) => ({ ...d, resetParams: false, params: { ...(d.params ?? savedParams), [k]: nv } }))}
                    onError={(m) =>
                      setParamErrors((e) => {
                        const { [`${rule.id}:${k}`]: _drop, ...rest } = e;
                        return m ? { ...rest, [`${rule.id}:${k}`]: m } : rest;
                      })
                    }
                  />
                </div>
              );
            })}
          </div>
        )}
        {rule.id === FOUND_BUG_RULE && editableLib && (
          <span className="text-small text-fg-2">Off: the default, {FOUND_BUG_DEFAULT}, is in force.</span>
        )}
        {field && gitops && (
          <GitopsField
            rule={rule}
            field={field}
            editable={editable}
            readOnlyText={gitopsReadOnly(gitops)}
            value={draft.gitops ?? currentValue(gitops, field)}
            onChange={(v) => setDraft({ gitops: v })}
          />
        )}
        {rule.control === "select" && <MemoryWriteModeControl rule={rule} />}
        {rule.id === "plan.budget_cap" && rule.used && <BudgetCapEditor />}
        {detail && <div className="border-t border-line pt-2">{detail(rule)}</div>}
      </div>

      {/* 3 · resolution, effect, history, and the pinned save area */}
      <aside data-testid="rules-dock-side" className="flex min-h-0 flex-col border-t border-line min-[900px]:border-l min-[900px]:border-t-0">
        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3">
          <div data-testid="rules-dock-chain" className="flex flex-col gap-1">
            <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>RESOLUTION</span>
            {(
              [
                ["project", "PROJECT"],
                ["org", orgName ? `ORG ${orgName}` : "ORG"],
                ["platform", "PLATFORM DEFAULT"],
              ] as const
            ).map(([id, label]) => (
              <span
                key={id}
                data-testid={`rules-dock-chain-${id}`}
                aria-current={winner === id ? "true" : undefined}
                className={cn(
                  MONO,
                  "rounded-[4px] border px-2 py-0.5 text-small",
                  winner === id ? "border-accent text-accent" : "border-transparent text-muted",
                )}
              >
                {label}
                {winner === id ? " · in force" : ""}
              </span>
            ))}
            <span className="text-small text-muted">{toggleSourceLabel(t?.on.source)}</span>
          </div>

          <div data-testid="rules-dock-effect" className="flex flex-col gap-0.5">
            <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>MEASURED EFFECT</span>
            {!isLibrary(rule) ? (
              <span className="text-small text-muted">Not measured: only library rules have an effect reading.</span>
            ) : effect.isLoading ? (
              <span className="text-small text-muted">loading…</span>
            ) : effect.isError || !effectRule ? (
              <span className="text-small text-muted" data-testid="rules-dock-effect-unavailable">
                Effect unavailable: it could not be read. That is not zero.
              </span>
            ) : (
              <>
                <span className={cn(MONO, "text-small text-fg-2")}>caught: {measured(effectRule.caught)}</span>
                <span className={cn(MONO, "text-small text-fg-2")}>
                  escapes before → after: {measured(effectRule.escape_rate.before)} → {measured(effectRule.escape_rate.after)}
                </span>
                <span className={cn(MONO, "text-small text-fg-2")}>cost, held seconds: {measured(effectRule.cost.held_seconds)}</span>
                <span className={cn(MONO, "text-small text-fg-2")}>overridden: {measured(effectRule.cost.overridden)}</span>
              </>
            )}
          </div>

          <div className="flex flex-col gap-0.5" data-testid="rules-dock-history">
            <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>HISTORY</span>
            {isLibrary(rule) && !rule.unmeasured_reason ? (
              <DockHistory ruleId={rule.id} />
            ) : (
              <span className="text-small leading-snug text-muted" data-testid="rules-dock-history-none">
                {builtin
                  ? "No history: a built-in rule has no setting anyone can change, so there is nothing to record."
                  : rule.unmeasured_reason
                    ? "No history: the pack refused this rule's template, so it cannot be set."
                    : "No rule history: this is a project setting, and its changes are not recorded as rule toggles."}
              </span>
            )}
          </div>
          <span className={cn(MONO, "text-meta text-muted-2")} data-testid="rules-dock-level">
            {LEVELS[level].word} · v{version}
          </span>
        </div>

        <div data-testid="rules-dock-save" className="flex flex-col gap-2 border-t border-line bg-surface-2 p-3">
          {builtin ? (
            <p className="text-small text-fg-2" data-testid="rules-dock-builtin-note">
              This rule is built in and can't be changed here.
            </p>
          ) : !dirty ? (
            <p className="text-small text-muted" data-testid="rules-dock-nochange">
              No changes. Edit a value to see the diff and save it with a reason.
            </p>
          ) : (
            <ul className="flex flex-col gap-0.5" data-testid="rules-dock-diff">
              {lines.map((l) => (
                <li key={l} className={cn(MONO, "break-all text-small text-fg-2")}>
                  {l}
                </li>
              ))}
            </ul>
          )}
          {!builtin && (
            <>
              {risks.length > 0 && (
                <label className="flex items-start gap-2 text-small text-[#ffb454]" data-testid="rules-dock-weakening">
                  <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
                  <span>WEAKENING — I understand the risk: {risks.join("; ")}</span>
                </label>
              )}
              {needsReason && (
                <label className="flex flex-col gap-1 text-small text-fg-2">
                  Reason (at least {MIN_REASON} characters)
                  <Input aria-label="Reason" value={reason} placeholder="Say why, in a sentence" onChange={(e) => setReason(e.target.value)} />
                </label>
              )}
              {firstParamError && (
                <p role="alert" className="text-small text-st-blocked">
                  {firstParamError}
                </p>
              )}
              {error && (
                <p role="alert" data-testid="rules-dock-error" className="text-small leading-snug text-st-blocked">
                  {error}
                </p>
              )}
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={!canSave || saving}
                  onClick={() => void save()}
                  data-testid="rules-dock-save-button"
                  className="h-7 rounded-[8px] bg-accent px-3 text-body font-semibold text-[#0a0c0e] disabled:opacity-50"
                >
                  {saving ? "Saving…" : "Save"}
                </button>
                <button
                  type="button"
                  disabled={!dirty || saving}
                  onClick={reset}
                  data-testid="rules-dock-discard"
                  className="h-7 rounded-[8px] border border-control px-3 text-body text-fg-2 disabled:opacity-50"
                >
                  Discard
                </button>
              </div>
            </>
          )}
          {leaveTo && (
            <div role="alertdialog" aria-label="Unsaved changes" data-testid="rules-dock-leave" className="flex flex-col gap-1.5 rounded-[8px] border border-accent p-2">
              <span className="text-small text-fg">You have unsaved changes to {rule.title}. Leave without saving?</span>
              <div className="flex gap-2">
                <button
                  type="button"
                  data-testid="rules-dock-leave-confirm"
                  onClick={() => {
                    const to = leaveTo;
                    reset();
                    setSaved(null);
                    setLeaveTo(null);
                    setRuleId(to);
                  }}
                  className="h-6 rounded-[8px] border border-control px-2 text-small text-fg"
                >
                  Discard and leave
                </button>
                <button type="button" data-testid="rules-dock-leave-stay" onClick={() => setLeaveTo(null)} className="h-6 rounded-[8px] border border-control px-2 text-small text-fg-2">
                  Keep editing
                </button>
              </div>
            </div>
          )}
        </div>
      </aside>
    </section>
  );
}

type Effective = { on?: unknown; params?: unknown };

/** What a recorded change did: the effective value before → after, one part per changed key. */
export function historyChange(meta: Record<string, unknown> | null): string[] {
  const b = meta?.effective_before as Effective | undefined;
  const a = meta?.effective_after as Effective | undefined;
  if (!b || !a || typeof b !== "object" || typeof a !== "object") return [];
  const out: string[] = [];
  const word = (v: unknown) => (v === true ? "ON" : v === false ? "OFF" : showValue(v as Params[string]));
  if (b.on !== a.on) out.push(`on/off: ${word(b.on)} → ${word(a.on)}`);
  const bp = (b.params ?? {}) as Params;
  const ap = (a.params ?? {}) as Params;
  for (const k of Array.from(new Set([...Object.keys(bp), ...Object.keys(ap)])).sort()) {
    if (JSON.stringify(bp[k]) !== JSON.stringify(ap[k])) out.push(`${k}: ${showValue(bp[k])} → ${showValue(ap[k])}`);
  }
  return out;
}

/** Who changed the rule, when, why, and before → after, newest first. */
function DockHistory({ ruleId }: { ruleId: string }) {
  const { activeId } = useProjectCtx();
  const { data } = useRuleHistory(activeId, ruleId, true);
  if (!data || data.length === 0) return <span className="text-small text-muted">No changes recorded.</span>;
  return (
    <ol className="flex flex-col gap-1" data-testid="rules-dock-history-list">
      {data.map((e) => {
        const why = typeof e.meta?.reason === "string" ? e.meta.reason : "";
        const change = historyChange(e.meta);
        return (
          <li key={e.id} className="flex flex-col text-small leading-snug text-muted" data-testid={`rules-dock-history-${e.id}`}>
            <span>
              {e.actor_label || e.actor_id} {relTime(e.ts) ?? "at an unknown time"}
              {why ? ` — “${why}”` : ""}
            </span>
            {change.length > 0 ? (
              change.map((c) => (
                <span key={c} className={cn(MONO, "break-all text-fg-2")}>
                  {c}
                </span>
              ))
            ) : (
              <span className={cn(MONO, "text-fg-2")}>no change to the effective value</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

/** The build stage's found-bug rule is missing from the pack: say which default is in force. */
function FoundBugAbsent({ stage, rules }: { stage: RuleStage; rules: RuleFlowRule[] }) {
  if (stage.id !== "build" || rules.some((r) => r.id === FOUND_BUG_RULE)) return null;
  return (
    <div className="flex flex-col gap-1 rounded-[8px] px-2 py-2" style={{ border: "1.5px dashed #626c75" }} data-testid="rules-found-bug-absent">
      <span className="text-body font-semibold">Found bugs</span>
      <span className={cn(MONO, "text-small text-fg-2")}>{FOUND_BUG_RULE}</span>
      <span className="text-small leading-snug text-muted">
        The installed pack has no {FOUND_BUG_RULE} template, so the default, {FOUND_BUG_DEFAULT}, is in force.
      </span>
    </div>
  );
}
