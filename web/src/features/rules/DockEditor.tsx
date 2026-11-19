/**
 * The docked rule editor (GRPH-1718, the owner's layout C). A stage opens as an editor under the
 * map: the stage's rules in a list, the current rule's form, and its resolution chain, measured
 * effect and history with the save area pinned under them. It replaces the full-width stage view
 * (GRPH-1659) and the stage editor's draft-for-the-whole-stage: one rule is edited at a time, and
 * saving goes through the all-or-nothing batch write (`PUT /rules/toggles`) with that rule's
 * whole params object. Built-in rules open read-only.
 */
import * as React from "react";
import { Link } from "react-router-dom";

import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { useConfig, useGitops, useRuleEffect, useRuleHistory, useRuleToggles, useSetRuleToggles, useUpdateGitops } from "@/lib/queries";
import { settingsPath } from "@/lib/routes";
import type { GitopsPatch, RuleEffectRule, RuleFlowRule, RuleMeasured, RuleStage, RuleToggle, RuleToggleBatchEntry } from "@/lib/types";

import {
  enforcerKindLabel,
  enforcerText,
  externalText,
  LEVELS,
  quietLine,
  ruleLevel,
  stageLevel,
  stageRules,
  valueText,
  weakestFirst,
} from "./levels";
import { cleanParams, FOUND_BUG_DEFAULT, FOUND_BUG_RULE, MEMORY_WRITE_MODES, MemoryWriteModeControl, useCanWriteRules } from "./LibraryRuleControls";
import { AdminGitopsLink, BudgetCapEditor, currentValue, GITOPS_RULE_FIELDS, toPatch, useOrgNameOrNull } from "./RuleEditors";
import { RuleLevelIcon } from "./RuleLevelIcon";
import { relTime } from "@/lib/relTime";
import { showValue, type Params } from "./RuleReviewSheet";
import { inheritsFor, ReadOnlyReason, SourceChip } from "./RuleSource";
import { enumFor, isScalarParam, RuleValueEditor } from "./RuleValueEditor";
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

const ACCENT = "#c6f24e";

/** "LIBRARY RULE · REVIEW", "BUILT-IN RULE · REVIEW", "PROJECT SETTING · MERGE". */
export function kindLine(r: RuleFlowRule, stage: RuleStage): string {
  const g = groupOf(r);
  const kind = g === "library" ? "LIBRARY RULE" : g === "project" ? "PROJECT SETTING" : "BUILT-IN RULE";
  return `${kind} · ${stage.title.toUpperCase()}`;
}

/** The form header's state badge: ON, NOT USED, or NOT USED · INHERITED when the off comes from above the project. */
export function stateBadge(r: RuleFlowRule, onSource: string | null | undefined): string {
  if (r.unmeasured_reason) return "UNMEASURED";
  if (!isLibrary(r)) return r.used ? "ON" : "NOT USED";
  if (r.value === true) return "ON";
  return onSource === "project" ? "NOT USED" : "NOT USED · INHERITED";
}

/** What the on/off resolves from, in one sentence under the state control. */
export function stateNote(on: boolean, source: string | null | undefined, orgName: string | null): string {
  if (source === "project") return on ? "This project switched it on." : "Off for this project, whatever the org sets.";
  if (source === "org") return `${orgName ? `The org ${orgName}` : "The org"} switched it ${on ? "on" : "off"}; this project follows it.`;
  return `Follows the org, then the platform default (${on ? "on" : "off"}).`;
}

/** Seconds held as "1h 48m", "12m", "40s". */
export function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h) return m ? `${h}h ${m}m` : `${h}h`;
  if (m) return `${m}m`;
  return `${s}s`;
}

const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
function adoptedText(at: string | null | undefined): string | null {
  if (!at) return null;
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? null : `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
}

type Draft = { on?: boolean; params?: Params; resetOn?: boolean; resetParams?: boolean; gitops?: string };
type DiffLine = { key: string; before: string; after: string };

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

/** A measured number, or the word for why there isn't one: never a bare 0 for a reading that was not taken. */
function measuredWord(m: RuleMeasured | undefined): string {
  if (!m || m.status === "unavailable") return "unavailable";
  if (m.status === "insufficient" || m.value === null) return "not enough data";
  return String(m.value);
}

const isMeasured = (m: RuleMeasured | undefined): m is RuleMeasured & { value: number } =>
  !!m && m.status === "measured" && m.value !== null;

/** The reasons behind the unmeasured readings, each said once. */
function reasonsOf(...ms: (RuleMeasured | undefined)[]): string | null {
  const out = new Set<string>();
  for (const m of ms) {
    if (isMeasured(m)) continue;
    out.add(
      m?.reason ||
        (!m ? "not reported" : m.status === "insufficient" ? `not enough data (${m.n ?? 0} of ${m.min_n})` : "the server gave no reason"),
    );
  }
  return out.size ? Array.from(out).join("; ") : null;
}

type Tile = { id: string; label: string; value: string; sub: string };

/** The four effect tiles (GRPH-1725): a large value each and one sub-line; an unmeasured tile states its reason once. */
export function effectTiles(fx: RuleEffectRule): Tile[] {
  const caught = fx.caught;
  const items = caught.items ?? [];
  const before = fx.escape_rate.before;
  const after = fx.escape_rate.after;
  const held = fx.cost.held_seconds;
  const over = fx.cost.overridden;
  const escapes =
    !isMeasured(before) && !isMeasured(after) && measuredWord(before) === measuredWord(after)
      ? measuredWord(before)
      : `${measuredWord(before)} → ${measuredWord(after)}`;
  return [
    {
      id: "caught",
      label: "CAUGHT",
      value: measuredWord(caught),
      sub: isMeasured(caught) ? (items.length ? items.join(", ") : "no item ids recorded") : reasonsOf(caught)!,
    },
    {
      id: "escapes",
      label: "ESCAPES",
      value: escapes,
      sub: reasonsOf(before, after) ?? "escape rate, before → after adoption",
    },
    {
      id: "cost",
      label: "COST",
      value: isMeasured(held) ? `${duration(held.value)} held · ${held.n ?? 0} ${held.n === 1 ? "item" : "items"}` : measuredWord(held),
      sub: isMeasured(held) ? "time work waited on this rule" : reasonsOf(held)!,
    },
    {
      id: "overridden",
      label: "OVERRIDDEN",
      value: measuredWord(over),
      sub: isMeasured(over) ? "refusals a person overrode" : reasonsOf(over)!,
    },
  ];
}

export function DockEditor({
  stage,
  version,
  model,
  focusRule,
  prefill,
  onClose,
  onDirty,
  onRule,
}: {
  stage: RuleStage;
  version: string;
  model: string | null;
  focusRule: string | null;
  prefill: { rule: string; value: string } | null;
  onClose: () => void;
  /** Told whenever the draft turns dirty or clean, so a stage switch can ask first. */
  onDirty?: (dirty: boolean) => void;
  /** Told which rule the form is on, so the dock header can name it. */
  onRule?: (rule: RuleFlowRule | null) => void;
}) {
  const { activeId } = useProjectCtx();
  const canWrite = useCanWriteRules(activeId) !== false;
  const { data: gitops } = useGitops(activeId);
  const { data: toggles } = useRuleToggles(activeId);
  const saveRules = useSetRuleToggles(activeId);
  const saveGitops = useUpdateGitops(activeId);
  const orgName = useOrgNameOrNull(gitops);
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
  React.useEffect(() => {
    onRule?.(rule);
  }, [rule, onRule]);
  const t: RuleToggle | undefined = toggles?.toggles.find((x) => x.rule_id === rule?.id);
  const effect = useRuleEffect(activeId, rule && isLibrary(rule) ? rule.id : null);
  const effectRule = effect.data?.rules.find((x) => x.rule_id === rule?.id);

  // A refusal's ?rule= lands keyboard focus on the rule's own header.
  const headRef = React.useRef<HTMLDivElement | null>(null);
  const focused = !!rule && focusRule === rule.id;
  React.useEffect(() => {
    if (!focused) return;
    headRef.current?.scrollIntoView?.({ block: "nearest" });
    headRef.current?.focus({ preventScroll: true });
  }, [focused, rule?.id]);

  const field = rule ? GITOPS_RULE_FIELDS[rule.id] : undefined;
  const gitopsEditable = !!gitops && gitops.control.writable && gitops.control.state === "local";
  const editableLib = !!rule && isEditableLibrary(rule);
  const editable = canWrite && (editableLib || (!!field && gitopsEditable && !!rule?.used));

  // The diff: one chip per changed key, before → after; and the batch entry that carries it.
  const { lines, entry, patch, risks } = React.useMemo(() => {
    const lines: DiffLine[] = [];
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
        lines.push({ key: "on/off", before: on ? "ON" : "OFF", after: `inherited (${inheritsFor(t.inherits?.on, orgName)})` });
      } else if (draft.on !== undefined && draft.on !== on) {
        e.on = draft.on;
        lines.push({ key: "on/off", before: on ? "ON" : "OFF", after: draft.on ? "ON" : "OFF" });
      }
      const writeParams = (cleaned: Params) => {
        const changed = Object.keys(cleaned).filter((k) => !sameValue(cleaned[k], current[k]));
        if (changed.length === 0) return;
        e.params = cleaned; // the whole object: the write replaces params, it does not merge
        for (const k of changed) {
          lines.push({ key: k, before: showValue(current[k]), after: showValue(cleaned[k]) });
          const after = cleaned[k];
          const opt = typeof after === "string" ? enumFor(rule.id, k)?.find((o) => o.value === after) : undefined;
          if (opt?.risky) risks.push(`${opt.label}: ${opt.explain}`);
        }
      };
      // GRPH-1727: the value a reset lands on, when the read carries it (older servers do not).
      const inherited = t?.inherited?.params as Params | undefined;
      const from = inheritsFor(t?.inherits?.params, orgName);
      if (draft.resetParams && t?.params.source === "project") {
        const edited = draft.params ? cleanParams(draft.params) : null;
        if (edited && inherited && !sameValue(edited, cleanParams(inherited))) {
          // Reset, then edited: the edit starts from the inherited value and is saved as the
          // project's own — e.g. the defaults plus one more path.
          writeParams(edited);
        } else {
          e.params = null;
          const keys = inherited ? [...new Set([...Object.keys(current), ...Object.keys(inherited)])] : [];
          const differ = keys.filter((k) => !sameValue(current[k], inherited?.[k]));
          if (inherited && differ.length > 0) {
            for (const k of differ) lines.push({ key: k, before: showValue(current[k]), after: `${showValue(inherited[k])} (inherited: ${from})` });
          } else {
            lines.push({ key: "params", before: "project values", after: `inherited (${from})` });
          }
        }
      } else if (draft.params) {
        writeParams(cleanParams(draft.params));
      }
      if (e.on !== undefined || e.params !== undefined) entry = e;
    } else if (field && gitops && draft.gitops !== undefined) {
      const before = currentValue(gitops, field);
      if (draft.gitops !== before) {
        Object.assign(patch, toPatch(field, draft.gitops));
        lines.push({ key: field, before: showValue(before), after: showValue(draft.gitops) });
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

  // Esc closes the dock back to the map, unless it is a field's own Escape; ⌘↵ / Ctrl+↵ saves.
  const onKeyDown = (e: React.KeyboardEvent) => {
    const el = e.target as HTMLElement;
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      if (canSave && !saving) {
        e.preventDefault();
        void save();
      }
      return;
    }
    if (e.key === "Escape" && !/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) {
      e.stopPropagation();
      onClose();
    }
  };

  async function save() {
    setError(null);
    const what = lines.map((l) => `${l.key} ${l.before} → ${l.after}`).join(", ");
    try {
      if (entry) await saveRules.mutateAsync({ toggles: [entry], reason: reason.trim(), ...(hasPatch ? { gitops: patch } : {}) });
      else if (hasPatch) await saveGitops.mutateAsync(patch);
    } catch (err) {
      const detail = errorDetail(err, "The server refused the change.");
      setError(entry && detail.startsWith(`${entry.rule_id}: `) ? detail.slice(entry.rule_id.length + 2) : detail);
      return;
    }
    reset();
    setSaved(`Saved by you · ${what} · in force on the next sign-off`);
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
  // GRPH-1727: after "Reset to inherited" the editors draw the inherited value, and an edit
  // starts from it — the reset is kept, not dropped. An older read without `inherited` falls
  // back to the project value, and an edit then replaces the reset as before.
  const inheritedParams = t?.params.source === "project" ? (t?.inherited?.params as Params | undefined) : undefined;
  const resetShown = !!draft.resetParams && !!inheritedParams;
  const baseParams: Params = resetShown && inheritedParams ? inheritedParams : savedParams;
  const params: Params = draft.params ?? baseParams;
  const paramKeys = resetShown && inheritedParams ? [...new Set([...Object.keys(savedParams), ...Object.keys(inheritedParams)])] : Object.keys(savedParams);
  const level = ruleLevel(rule);
  const meta = LEVELS[level];
  const builtin = groupOf(rule) === "builtin";
  const library = isLibrary(rule);
  const fixed = FIXED_RULE_REASONS[rule.id];
  const onNow = draft.on ?? rule.value === true;
  const segment = draft.resetOn ? "inherit" : onNow ? "on" : "off";
  const onSource = t?.on.source ?? (library ? rule.source : null);
  const noteSource = draft.resetOn ? "inherit" : draft.on !== undefined ? "project" : onSource;
  // Gitops settings and select rules have no on/off of their own: no state control for them.
  const showState = library || builtin;
  const dim = !rule.used && !rule.unmeasured_reason;
  const quiet = quietLine(rule);
  const select_ = rule.control === "select";
  const budget = rule.id === "plan.budget_cap";
  const settingValue = library
    ? null
    : select_
      ? MEMORY_WRITE_MODES.find((m) => m.value === String(rule.value ?? "review"))?.label ?? String(rule.value ?? "review")
      : rule.truth === "db"
        ? valueText(rule.value) ?? (budget ? null : "— no value")
        : null;
  const nChanges = lines.length;

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
                const lv = ruleLevel(r);
                const word = stateWord(r);
                const lit = word.startsWith("ON");
                return (
                  <button
                    key={r.id}
                    type="button"
                    aria-current={on ? "true" : undefined}
                    data-testid={`rules-dock-row-${r.id}`}
                    data-level={lv}
                    onClick={() => select(r.id)}
                    className={cn("flex items-start gap-2 rounded-[8px] px-2 py-1.5 text-left hover:bg-surface-2", on && "bg-surface-2")}
                    style={{ boxShadow: on ? `inset 3px 0 0 ${ACCENT}` : undefined }}
                  >
                    <span className="mt-0.5 flex-none" style={{ color: LEVELS[lv].color }} data-testid={`rules-dock-rowicon-${r.id}`}>
                      <RuleLevelIcon level={lv} size={14} />
                    </span>
                    <span className="flex min-w-0 flex-col gap-0.5">
                      <span className="text-body font-semibold leading-snug text-fg">{r.title}</span>
                      <span className={cn(MONO, "break-all text-meta text-muted-2")} data-testid={`rules-dock-rowid-${r.id}`}>
                        {r.id}
                      </span>
                      <span
                        className={cn(MONO, "text-meta tracking-[0.05em]")}
                        data-testid={`rules-dock-rowstate-${r.id}`}
                        data-lit={lit ? "true" : undefined}
                        style={{ color: lit ? ACCENT : "#8b949e" }}
                      >
                        {word}
                      </span>
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
        <div
          ref={headRef}
          data-testid={`rules-row-${rule.id}`}
          data-level={level}
          data-focused={focused ? "true" : undefined}
          aria-current={focused ? "true" : undefined}
          tabIndex={focused ? -1 : undefined}
          className="flex flex-col gap-1.5 outline-none"
          style={focused ? { borderLeft: `4px solid ${ACCENT}`, paddingLeft: 8 } : undefined}
        >
          <div className="flex flex-wrap items-center gap-2" data-testid="rules-dock-head">
            <span
              data-testid="rules-dock-level-pill"
              data-level={level}
              className={cn(MONO, "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-meta font-semibold tracking-[0.06em]")}
              style={{ border: meta.pillBorder, background: meta.pillBg, color: meta.color }}
            >
              <RuleLevelIcon level={level} size={12} />
              {meta.word}
            </span>
            <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")} data-testid="rules-dock-kind">
              {kindLine(rule, stage)}
            </span>
            <span className="flex-1" />
            <span
              data-testid="rules-dock-state-badge"
              className={cn(MONO, "rounded-[4px] border px-1.5 text-meta font-semibold tracking-[0.05em]")}
              style={
                stateBadge(rule, onSource) === "ON"
                  ? { borderColor: "rgba(198,242,78,.4)", color: ACCENT }
                  : { borderColor: "#2a333a", color: "#8b949e" }
              }
            >
              {stateBadge(rule, onSource)}
            </span>
          </div>
          <span className="text-lead font-semibold leading-tight" style={{ opacity: dim ? 0.7 : 1 }}>
            {rule.title}
          </span>
          <span className={cn(MONO, "break-all text-small text-fg-2")}>{rule.id}</span>
          <span className="text-body leading-normal text-muted">{rule.summary}</span>
          <span className="flex flex-wrap items-center gap-1.5 text-small text-muted" data-testid="rules-dock-enforced-by">
            Enforced by
            <EnforcerLink kind={rule.enforcer.kind} text={enforcerText(rule, version)} url={rule.enforcer.url} />
          </span>
          {rule.used && rule.enforcement === "checked" && <LastResult rule={rule} />}
          {rule.used && rule.enforcement === "external" && (
            <span className="text-small leading-snug text-fg-2">
              <span className={cn(MONO, "mr-2 text-meta font-semibold tracking-[0.06em] text-[#c9b8ff]")}>NOT VERIFIED</span>
              {externalText(rule)}
            </span>
          )}
          {quiet && <span className="text-small leading-snug text-muted-2">{quiet}</span>}
          {settingValue !== null && (
            <span className="flex flex-wrap items-baseline gap-2">
              <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>IN FORCE</span>
              <span
                data-testid="rules-value"
                className={cn(MONO, "break-words text-body font-semibold")}
                style={{ color: settingValue === "— no value" ? "#ff6b6b" : "#e6e9ec" }}
              >
                {settingValue}
              </span>
            </span>
          )}
          {dim && (
            <span className="text-small leading-snug text-muted" data-testid="rules-row-not-used">
              {rule.not_used_reason
                ? `NOT USED — ${rule.not_used_reason}.`
                : library
                  ? `NOT USED — ${rule.label ?? "turned off for this project"}. On, it is ${LEVELS[rule.enforcement].word}.`
                  : `Not used by this project's model${model ? `: ${model}` : ""}.`}
            </span>
          )}
          {rule.id === "gitops.model" && <ModelWhere />}
          {rule.unmeasured_reason && (
            <span data-testid="rules-toggle-refused" className="text-small leading-snug text-[#ff6b6b]">
              UNMEASURED · TEMPLATE REFUSED — {rule.unmeasured_reason}
            </span>
          )}
          {rule.locked && (
            <span data-testid="rules-toggle-locked" className="text-small leading-snug text-fg-2">
              Locked: the org set this for every project; change it on the org console.
            </span>
          )}
          {rule.id === FOUND_BUG_RULE && library && !rule.unmeasured_reason && (
            <span data-testid="rules-found-bug-effective" className="text-small leading-snug text-fg-2">
              {rule.value === true
                ? `Effective mode: ${modeLabel(rule.id, (rule.params?.value as Params | null)?.mode ?? FOUND_BUG_DEFAULT)}.`
                : `Off: the default, ${FOUND_BUG_DEFAULT}, is in force.`}
            </span>
          )}
        </div>
        {stageLevel(stage) === "not_used" && (
          <p className="text-small leading-snug text-muted" data-testid="rules-stage-not-used">
            Every rule on this stage is not used by this project's model{model ? `: ${model}` : ""}. They stay drawn so you can see what would apply.
          </p>
        )}
        {fixed && <ReadOnlyReason>{fixed}</ReadOnlyReason>}

        {showState && (
          <div className="flex flex-col gap-1">
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
            {library && !rule.unmeasured_reason && (
              <span className="text-small leading-snug text-muted" data-testid="rules-dock-state-note">
                {noteSource === "inherit"
                  ? `Back to inherited: ${inheritsFor(t?.inherits?.on, orgName)}.`
                  : stateNote(onNow, noteSource, orgName)}
              </span>
            )}
          </div>
        )}

        {builtin && <ReadOnlyReason>Built in: this holds by how Graphban works, so every control here is read-only.</ReadOnlyReason>}

        {editableLib && paramKeys.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <table className="w-full table-fixed border-collapse overflow-hidden rounded-[8px] border border-[#2a333a] text-left" data-testid="rules-dock-params">
              <colgroup>
                <col className="w-[38%]" />
                <col />
                <col className="w-[26%]" />
              </colgroup>
              <thead>
                <tr className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>
                  <th className="px-2.5 py-1.5 font-normal">NAME</th>
                  <th className="px-2.5 py-1.5 font-normal">VALUE</th>
                  <th className="px-2.5 py-1.5 font-normal">SET BY</th>
                </tr>
              </thead>
              <tbody>
                {paramKeys.map((k) => {
                  const v = savedParams[k];
                  const shown = params[k] ?? v;
                  const changed = JSON.stringify(shown) !== JSON.stringify(v);
                  const scalar = isScalarParam(rule.id, k, v);
                  const editor = (
                    <RuleValueEditor
                      key={epoch}
                      ruleId={rule.id}
                      name={k}
                      value={shown}
                      hideLabel
                      compact
                      effectivePredicates={t?.effective_predicates}
                      onChange={(nv) =>
                        setDraft((d) => {
                          const keep = !!d.resetParams && !!inheritedParams;
                          const base = keep && inheritedParams ? inheritedParams : savedParams;
                          return { ...d, resetParams: keep, params: { ...(d.params ?? base), [k]: nv } };
                        })
                      }
                      onError={(m) =>
                        setParamErrors((e) => {
                          const { [`${rule.id}:${k}`]: _drop, ...rest } = e;
                          return m ? { ...rest, [`${rule.id}:${k}`]: m } : rest;
                        })
                      }
                    />
                  );
                  const bg = changed ? "rgba(198,242,78,.05)" : undefined;
                  return (
                    <React.Fragment key={k}>
                      <tr
                        data-testid={`rules-dock-param-${k}`}
                        data-changed={changed ? "true" : undefined}
                        className="border-t border-line align-top"
                        style={{ background: bg }}
                      >
                        <td className="px-2.5 py-2">
                          <span className={cn(MONO, "block break-all text-body font-semibold")}>{k}</span>
                          <span className="block text-small leading-snug text-muted" data-testid={`rules-dock-help-${k}`}>
                            {paramHelp(rule.id, k)}
                          </span>
                        </td>
                        <td className="px-2.5 py-2" data-testid={`rules-dock-value-${k}`}>
                          {scalar ? editor : <span className="text-small text-muted">see below</span>}
                        </td>
                        <td className="px-2.5 py-2">
                          <span className="flex flex-col items-start gap-1">
                            <SourceChip source={t?.params.source} locked={rule.locked ?? false} testId={`rules-dock-source-${k}`} />
                            {resetShown && (
                              <span className="text-small text-muted" data-testid={`rules-dock-inherited-${k}`}>
                                inherited from {inheritsFor(t?.inherits?.params, orgName)}
                              </span>
                            )}
                            {changed && <span className={cn(MONO, "break-all text-small text-muted")}>was {showValue(v)}</span>}
                            {t?.params.source === "project" && !draft.resetParams && (
                              <button
                                type="button"
                                disabled={!editable}
                                data-testid={`rules-dock-reset-${k}`}
                                onClick={() => {
                                  setDraft((d) => ({ ...d, resetParams: true, params: undefined }));
                                  setEpoch((n) => n + 1); // remount the editors on the inherited value
                                }}
                                className="text-left text-small text-accent underline disabled:opacity-50"
                              >
                                Reset to inherited
                              </button>
                            )}
                          </span>
                        </td>
                      </tr>
                      {!scalar && (
                        <tr className="align-top" style={{ background: bg }} data-testid={`rules-dock-param-wide-${k}`}>
                          <td colSpan={3} className="px-2.5 pb-2.5">
                            {editor}
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
            <span className="text-small text-muted" data-testid="rules-dock-params-note">
              Only the template's own parameters can be set. The server refuses any other.
            </span>
          </div>
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
        {select_ && <MemoryWriteModeControl rule={rule} />}
        {budget && rule.used && <BudgetCapEditor />}
      </div>

      {/* 3 · resolution, effect, history, and the pinned save area */}
      <aside data-testid="rules-dock-side" className="flex min-h-0 flex-col border-t border-line min-[900px]:border-l min-[900px]:border-t-0">
        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3">
          <Resolution rule={rule} t={t} orgName={orgName} />

          <div data-testid="rules-dock-effect" className="flex flex-col gap-1.5">
            <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")} data-testid="rules-dock-effect-heading">
              MEASURED EFFECT{adoptedText(effectRule?.adopted_at) ? ` · ADOPTED ${adoptedText(effectRule?.adopted_at)}` : ""}
            </span>
            {!library ? (
              <span className="text-small text-muted">Not measured: only library rules have an effect reading.</span>
            ) : effect.isLoading ? (
              <span className="text-small text-muted">loading…</span>
            ) : effect.isError || !effectRule ? (
              <span className="text-small text-muted" data-testid="rules-dock-effect-unavailable">
                Effect unavailable: it could not be read. That is not zero.
              </span>
            ) : (
              <>
                <div className="grid grid-cols-2 gap-1.5">
                  {effectTiles(effectRule).map((tile) => (
                    <div
                      key={tile.id}
                      data-testid={`rules-dock-tile-${tile.id}`}
                      className="flex min-w-0 flex-col gap-0.5 rounded-[8px] border border-[#2a333a] bg-surface-2 px-2 py-1.5"
                    >
                      <span className={cn(MONO, "text-micro tracking-[0.07em] text-muted-2")}>{tile.label}</span>
                      <span className={cn(MONO, "break-words text-lead font-semibold leading-tight text-fg")} data-testid={`rules-dock-tile-${tile.id}-value`}>
                        {tile.value}
                      </span>
                      <span className="break-words text-micro leading-snug text-muted" data-testid={`rules-dock-tile-${tile.id}-sub`}>
                        {tile.sub}
                      </span>
                    </div>
                  ))}
                </div>
                <span className="text-micro leading-snug text-muted-2" data-testid="rules-dock-effect-window">
                  {effectRule.escape_note ||
                    "Escapes compare the window after adoption with an equal window before it. Observed, not a cause."}
                </span>
              </>
            )}
          </div>

          <div className="flex flex-col gap-0.5" data-testid="rules-dock-history">
            <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>HISTORY</span>
            {library && !rule.unmeasured_reason ? (
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
          ) : dirty ? (
            <ul className="flex flex-wrap gap-1.5" data-testid="rules-dock-diff">
              {lines.map((l) => (
                <li
                  key={l.key}
                  data-testid={`rules-dock-diff-${l.key}`}
                  className={cn(MONO, "inline-flex max-w-full flex-wrap items-baseline gap-1 rounded-[6px] border border-[#2a333a] bg-bg px-1.5 py-0.5 text-small")}
                >
                  <span className="text-fg">{l.key}</span>{" "}
                  <s className="break-all text-muted">{l.before}</s> <span aria-hidden className="text-muted-2">→</span>{" "}
                  <span className="break-all text-accent">{l.after}</span>
                </li>
              ))}
            </ul>
          ) : saved ? (
            <p role="status" data-testid="rules-dock-saved" className="flex items-start gap-1.5 text-small text-fg-2">
              <span aria-hidden className="text-accent">✓</span>
              <span>{saved}</span>
            </p>
          ) : (
            <p className="text-small text-muted" data-testid="rules-dock-nochange">
              No changes. Edit a value to see the diff and save it with a reason.
            </p>
          )}
          {!builtin && (
            <>
              {risks.length > 0 && (
                <label className="flex items-start gap-2 text-small text-[#ffb454]" data-testid="rules-dock-weakening">
                  <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
                  <span>WEAKENING — I understand the risk: {risks.join("; ")}</span>
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
              <div className="flex flex-wrap items-center gap-2" data-testid="rules-dock-save-row">
                {needsReason && (
                  <Input
                    aria-label="Reason"
                    value={reason}
                    className="h-7 min-w-[160px] flex-1 text-small"
                    placeholder="Why? One sentence, saved as the audit record (required)"
                    onChange={(e) => setReason(e.target.value)}
                  />
                )}
                <button
                  type="button"
                  disabled={!canSave || saving}
                  onClick={() => void save()}
                  data-testid="rules-dock-save-button"
                  title="⌘↵ / Ctrl+↵"
                  className="h-7 flex-none rounded-[8px] bg-accent px-3 text-body font-semibold text-[#0a0c0e] disabled:opacity-50"
                >
                  {saving ? "Saving…" : nChanges ? `Save ${nChanges} change${nChanges === 1 ? "" : "s"}` : "Save"}
                </button>
                <button
                  type="button"
                  disabled={!dirty || saving}
                  onClick={reset}
                  data-testid="rules-dock-discard"
                  className="h-7 flex-none rounded-[8px] border border-control px-3 text-body text-fg-2 disabled:opacity-50"
                >
                  Discard
                </button>
              </div>
              {needsReason && reason.length > 0 && !reasonOk && (
                <span className="text-micro text-muted" data-testid="rules-dock-reason-short">
                  At least {MIN_REASON} characters.
                </span>
              )}
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

/** Equal param values, key order aside (a map read back from JSON may reorder). */
function sameValue(a: unknown, b: unknown): boolean {
  const norm = (x: unknown): unknown =>
    x && typeof x === "object" && !Array.isArray(x)
      ? Object.fromEntries(Object.entries(x as Record<string, unknown>).sort(([p], [q]) => p.localeCompare(q)).map(([k, v]) => [k, norm(v)]))
      : x;
  return JSON.stringify(norm(a)) === JSON.stringify(norm(b));
}

function modeLabel(ruleId: string, v: Params[string]): string {
  return typeof v === "string" ? enumFor(ruleId, "mode")?.find((o) => o.value === v)?.label ?? v : showValue(v);
}

/** One level's value as the mock writes it: "on · min_tests_failed 2". */
function levelValue(on: boolean | null, params: Params | null): string {
  const parts: string[] = [];
  if (on !== null) parts.push(on ? "on" : "off");
  if (params) for (const [k, v] of Object.entries(params)) parts.push(`${k} ${showValue(v)}`);
  return parts.join(" · ");
}

/**
 * Project → org → platform default, each with its value and the winner IN FORCE. The toggles read
 * resolves each field to the first level that sets it, so a level's value is known where it won;
 * the org's row is otherwise known only to exist (`inherits`), and is said that way.
 */
export function Resolution({ rule, t, orgName }: { rule: RuleFlowRule; t: RuleToggle | undefined; orgName: string | null }) {
  const library = isLibrary(rule);
  // With no toggles read, the flow's own sources stand in, so the chain and the state note agree.
  const norm = (x: string | null | undefined) => (x === "project" || x === "org" ? x : "platform default");
  const onSrc = library ? norm(t?.on.source ?? rule.source) : null;
  const parSrc = library ? norm(t?.params.source ?? rule.params?.source ?? rule.source) : null;
  const onVal = t ? t.on.value : rule.value === true;
  const parVal = (t ? t.params.value : (rule.params?.value as Params | null)) ?? {};
  const has = (src: string) => onSrc === src || parSrc === src;
  const winner: "project" | "org" | "platform" = library
    ? has("project")
      ? "project"
      : has("org")
        ? "org"
        : "platform"
    : rule.source === "project"
      ? "project"
      : rule.source === "org"
        ? "org"
        : "platform";
  const at = (src: string) =>
    levelValue(onSrc === src ? onVal : null, parSrc === src && Object.keys(parVal).length ? (parVal as Params) : null);
  const setting = library ? null : valueText(rule.value) ?? (rule.control === "select" ? String(rule.value ?? "review") : null);
  const orgHasRow = t?.inherits?.on === "org" || t?.inherits?.params === "org";

  const steps: { id: "project" | "org" | "platform"; label: string; value: string }[] = [
    {
      id: "project",
      label: "PROJECT",
      value: library ? at("project") || "not set" : winner === "project" ? setting ?? "set" : "not set",
    },
    {
      id: "org",
      label: orgName ? `ORG ${orgName}` : "ORG · none",
      value: !orgName
        ? ""
        : library
          ? at("org") || (orgHasRow ? "set · overridden" : "not set")
          : winner === "org"
            ? setting ?? "set"
            : "not set",
    },
    {
      id: "platform",
      label: "PLATFORM DEFAULT",
      value: library ? at("platform default") || "overridden" : winner === "platform" ? setting ?? (groupOf(rule) === "builtin" ? "built in" : "default") : "overridden",
    },
  ];
  return (
    <div data-testid="rules-dock-chain" className="flex flex-col gap-1">
      <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>RESOLUTION</span>
      {steps.map((s) => (
        <span
          key={s.id}
          data-testid={`rules-dock-chain-${s.id}`}
          aria-current={winner === s.id ? "true" : undefined}
          className={cn(
            MONO,
            "flex flex-wrap items-baseline gap-x-1.5 rounded-[4px] border px-2 py-0.5 text-small",
            winner === s.id ? "border-accent text-accent" : "border-transparent text-muted",
          )}
        >
          <span className="font-semibold">{s.label}</span>
          {s.value && (
            <span className={winner === s.id ? "text-fg" : "text-muted"} data-testid={`rules-dock-chain-${s.id}-value`}>
              {s.value}
            </span>
          )}
          {winner === s.id && <span className="ml-auto text-meta tracking-[0.06em]">IN FORCE</span>}
        </span>
      ))}
    </div>
  );
}

function LastResult({ rule }: { rule: RuleFlowRule }) {
  if (!rule.last_result) {
    // No attestation on a merged item: said as such, never drawn as a pass.
    return (
      <span data-testid="rules-no-result" className="text-small text-muted">
        <span className={cn(MONO, "mr-2 text-meta font-semibold tracking-[0.06em] text-fg-2")}>NO RESULT RECORDED</span>
        Nothing has reported yet. That is not a pass.
      </span>
    );
  }
  return (
    <span data-testid="rules-result" data-outcome={rule.last_result.outcome} className="text-small text-muted">
      <span className={cn(MONO, "mr-2 text-meta font-semibold tracking-[0.06em] text-fg-2")}>
        {rule.last_result.outcome === "pass" ? "PASS" : "FAIL"} · last attested {relTime(rule.last_result.at) ?? "at an unknown time"}
      </span>
      {rule.last_result.item}
    </span>
  );
}

function ModelWhere() {
  const { data: config } = useConfig();
  const hosted = config?.hosted_mode ?? false;
  return (
    <span className="text-small leading-snug text-muted" data-testid="rules-gitops-model-where">
      A preset rewrites other fields, so the model is not edited inline. Change it in{" "}
      {hosted ? (
        <AdminGitopsLink />
      ) : (
        <Link to={settingsPath("deployment/gitops")} className="text-accent hover:underline">
          Settings → Gitops
        </Link>
      )}
    </span>
  );
}

export function EnforcerLink({ kind, text, url }: { kind: string; text: string; url: string }) {
  const body = (
    <>
      <span className="flex-none rounded-[3px] border border-[#2a333a] px-1 text-micro tracking-[0.05em] text-muted">
        {enforcerKindLabel(kind)}
      </span>
      <span>{text}</span>
    </>
  );
  const cls = cn(MONO, "inline-flex items-center gap-1.5 break-all text-small leading-snug");
  // No URL from the server means no link — a guessed one would read as a verified source.
  if (!url) return <span className={cn(cls, "text-fg-2")} data-testid="rules-enforcer">{body}</span>;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer" className={cn(cls, "text-accent hover:underline")} data-testid="rules-enforcer">
      {body}
      <span aria-hidden>↗</span>
    </a>
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
