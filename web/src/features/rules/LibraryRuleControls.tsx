/**
 * Library rule toggles on the Rules inspector (GRPH-1268 / PRD-P63 §8, S2c).
 *
 * A library rule row carries an on/off toggle, its params inline and the source of each value
 * (`project` / `org` / `platform default`). Its level is the primitive's, which the server
 * already draws; an off rule stays listed as NOT USED. Every change is a person's and needs a
 * reason, which is the audit record (D10), so the row asks for one before it writes and then
 * says who changed it and when. There is deliberately no bulk "enable all": adopting a rule is
 * one decision per rule. Writes go through `PUT /api/rules/toggles/{id}`. An org lock is set
 * on the console, not here.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { RefusalRuleLink } from "@/features/activity/RefusalRuleLink";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { consoleHref, settingsPath } from "@/lib/routes";
import { errorDetail, errorRuleId, isRefusal } from "@/lib/errors";
import {
  useGitops,
  useMe,
  useMembers,
  useRuleToggles,
  useSetMemoryWriteMode,
  useSetRuleToggle,
} from "@/lib/queries";
import type { RuleFlowRule, RuleParamValue, RuleToggleWrite, TestScopeMode } from "@/lib/types";

import { useOrgName } from "./RuleEditors";
import { RuleHistory } from "./RuleHistory";
import { inheritsFor, ReadOnlyReason, RuleSource } from "./RuleSource";
import { registerEnum, RuleValueEditor } from "./RuleValueEditor";

/** GRPH-1405: the test-scope rule's one choice. Mirrors `rule_toggles.TEST_SCOPE_MODES`. */
export const TEST_SCOPE_RULE = "build.test_scope";
export const TEST_SCOPE_MODES: { value: TestScopeMode; label: string; explain: string }[] = [
  { value: "agent_full", label: "The agent runs CI", explain: "The builder runs the full suites; the PR's CI is not required." },
  { value: "agent_and_pr", label: "The agent and the PR run CI", explain: "The builder runs the full suites and the PR's CI runs them again." },
  {
    value: "agent_targeted",
    label: "The agent runs partial CI, only what changed",
    explain: "The builder runs the tests it changed, the module's tests and the sabotage check; the PR's CI runs the full suites.",
  },
];

registerEnum(TEST_SCOPE_RULE, "mode", TEST_SCOPE_MODES.map((m) => ({ ...m, risky: m.value === "agent_targeted" })), "Where the test suites run");

const MONO = "font-mono";
const note = "text-[11.5px] leading-snug text-muted";

type Params = Record<string, RuleParamValue>;
type Pending = { body: Omit<RuleToggleWrite, "reason">; what: string };

export function LibraryRuleControls({ rule }: { rule: RuleFlowRule }) {
  if (rule.unmeasured_reason) {
    // Never half-enforced: a refused template has no toggle and no params to set.
    return (
      <div
        data-testid="rules-toggle-refused"
        className="flex flex-col gap-1 rounded-[8px] px-2.5 py-2"
        style={{ border: "1.5px dashed #ff6b6b" }}
      >
        <span className={cn(MONO, "text-[10px] font-semibold tracking-[0.06em] text-[#ff6b6b]")}>
          UNMEASURED · TEMPLATE REFUSED
        </span>
        <span className="text-[11.5px] leading-snug text-fg-2">{rule.unmeasured_reason}</span>
        <span className={note}>The rule pack loader refused this template, so it can't be turned on.</span>
      </div>
    );
  }
  return <ToggleBody rule={rule} />;
}

export const MEMORY_WRITE_MODES: { value: string; label: string; explain: string }[] = [
  { value: "review", label: "Review", explain: "Every candidate waits for a person or an independent judge." },
  { value: "auto", label: "Auto", explain: "A candidate the judge scores high enough is published; the rest wait." },
  { value: "trusted", label: "Trusted", explain: "Candidates are published with no judge and no person assessing them." },
];

/** `memory.write_mode` (GRPH-1497): the project's mode and what it means; a change needs a reason. */
export function MemoryWriteModeControl({ rule }: { rule: RuleFlowRule }) {
  const { activeId } = useProjectCtx();
  const current = String(rule.value ?? "review");
  const [pending, setPending] = React.useState<string | null>(null);
  const [reason, setReason] = React.useState("");
  const [error, setError] = React.useState<unknown>(null);
  const mut = useSetMemoryWriteMode(activeId);
  return (
    <div className="flex flex-col gap-2 rounded-[8px] border border-[#2a333a] bg-bg p-2.5" data-testid="rules-write-mode">
      <div role="radiogroup" aria-label="Memory write mode" className="flex flex-col gap-1.5">
        {MEMORY_WRITE_MODES.map((m) => (
          <label key={m.value} className="flex items-start gap-2 text-[12px] text-fg-2">
            <input
              type="radio"
              name="memory-write-mode"
              aria-label={m.label}
              checked={current === m.value}
              disabled={mut.isPending}
              onChange={() => {
                setPending(m.value);
                setReason("");
                setError(null);
              }}
            />
            <span>
              <span className="text-fg">{m.label}</span> — {m.explain}
            </span>
          </label>
        ))}
      </div>
      {pending && (
        <ReasonForm
          what={`Set mode to ${pending}`}
          reason={reason}
          onReason={setReason}
          saving={mut.isPending}
          onSubmit={() =>
            reason.trim() &&
            mut.mutate(
              { mode: pending, reason: reason.trim() },
              { onSuccess: () => setPending(null), onError: (e) => setError(e) },
            )
          }
          onCancel={() => {
            setPending(null);
            setError(null);
          }}
        />
      )}
      {error !== null && <ToggleError err={error} />}
      <RuleHistory ruleId={rule.id} />
    </div>
  );
}

/** Mirrors `rule_toggles.PROJECT_ADMIN_ROLES`: the roles that may set a project's rule toggle. */
export const RULE_WRITER_ROLES = ["owner", "admin"];

/**
 * Whether this person can write the project's toggles, known up front so a member who can't
 * sees text with a link instead of live editors that end in a 403 (`rules.toggle_needs_admin`).
 * `null` while either read is outstanding or failed: the editors show and the server decides.
 */
export function useCanWriteRules(projectId: string): boolean | null {
  const { data: me } = useMe();
  const { data: members } = useMembers(projectId);
  if (!me || !members) return null;
  const role = members.find((m) => m.user.id === me.id)?.role;
  return !!role && RULE_WRITER_ROLES.includes(role);
}

function ToggleBody({ rule }: { rule: RuleFlowRule }) {
  const { activeId } = useProjectCtx();
  const canWrite = useCanWriteRules(activeId);
  const { data: gitops } = useGitops(activeId);
  const { data: toggles } = useRuleToggles(activeId);
  const toggle = toggles?.toggles.find((t) => t.rule_id === rule.id);
  const syncName = useOrgName(gitops);
  const orgName = syncName === "your org" && toggle?.locked_by ? toggle.locked_by : syncName;

  const on = rule.value === true;
  const params: Params = (rule.params?.value as Params | null) ?? {};
  const locked = !!rule.locked;
  const notWritable = !locked && canWrite === false;
  const readOnly = locked || notWritable;

  const [draftParams, setDraftParams] = React.useState<Params>(() => structuredClone(params));
  const [paramErrors, setParamErrors] = React.useState<Record<string, string>>({});
  const [pending, setPending] = React.useState<Pending | null>(null);
  const [reason, setReason] = React.useState("");
  const setProject = useSetRuleToggle(activeId);
  const saving = setProject.isPending;
  const [error, setError] = React.useState<unknown>(null);

  // A refetched flow (another person's change, or ours) resets the param inputs to what holds.
  const paramsKey = JSON.stringify(params);
  React.useEffect(() => {
    setDraftParams(JSON.parse(paramsKey) as Params);
    setParamErrors({});
  }, [paramsKey]);

  function ask(p: Pending) {
    setPending(p);
    setReason("");
    setError(null);
  }

  function submit() {
    if (!pending || !reason.trim()) return;
    const body: RuleToggleWrite = { ...pending.body, reason: reason.trim() };
    const opts = {
      onSuccess: () => {
        setPending(null);
        setReason("");
      },
      onError: (err: unknown) => setError(err),
    };
    setProject.mutate({ ruleId: rule.id, body }, opts);
  }

  const cleaned = cleanParams(draftParams);
  const firstError = Object.values(paramErrors)[0];
  const paramsDirty = !firstError && JSON.stringify(cleaned) !== paramsKey;
  const inherits = inheritsFor(toggle?.params.inherits, orgName);
  const onInherits = inheritsFor(toggle?.on.inherits, orgName);

  return (
    <div className="flex flex-col gap-2 rounded-[8px] border border-[#2a333a] bg-bg p-2.5" data-testid="rules-toggle">
      <div className="flex items-center gap-2">
        <span className={cn(MONO, "text-[10px] text-muted-2")}>{on ? "ON" : "OFF"}</span>
        <span className="flex-1" />
        <RuleSource
          source={rule.source}
          locked={locked}
          testId="rules-toggle-source"
          inherits={onInherits}
          disabled={saving || !!pending}
          onReset={readOnly ? undefined : () => ask({ body: { on: null }, what: `Reset to ${onInherits}` })}
        />
      </div>

      {locked ? (
        <span data-testid="rules-toggle-locked">
          <ReadOnlyReason href={consoleHref("/rules")} linkText="Change it on the org console">
            set by org {orgName} · locked.
          </ReadOnlyReason>
        </span>
      ) : notWritable ? (
        <span data-testid="rules-toggle-not-writable">
          <ReadOnlyReason href={settingsPath("project/members")} linkText="Ask an owner or admin">
            {on ? "On" : "Off"} · only the project's owner or an admin can change this rule.
          </ReadOnlyReason>
        </span>
      ) : (
        <button
          type="button"
          role="switch"
          aria-checked={on}
          aria-label={`${rule.title}: ${on ? "on" : "off"}`}
          disabled={saving}
          onClick={() => ask({ body: { on: !on }, what: on ? "Turn off" : "Turn on" })}
          data-testid="rules-toggle-switch"
          className={cn(
            "flex h-[22px] w-[40px] items-center rounded-full border px-[2px] transition-colors",
            on ? "justify-end border-accent bg-accent/20" : "justify-start border-control bg-surface-2",
          )}
        >
          <span className={cn("h-4 w-4 rounded-full", on ? "bg-accent" : "bg-muted")} />
        </button>
      )}

      {Object.keys(params).length > 0 && (
        <div className="flex flex-col gap-1.5" data-testid="rules-toggle-params">
          <div className="flex items-center gap-2">
            <span className={cn(MONO, "text-[10px] text-muted-2")}>params</span>
            <span className="flex-1" />
            <RuleSource
              source={rule.params?.source}
              locked={locked}
              testId="rules-toggle-params-source"
              inherits={inherits}
              disabled={saving || !!pending}
              onReset={readOnly ? undefined : () => ask({ body: { params: null }, what: `Reset to ${inherits}` })}
            />
          </div>
          {Object.entries(params).map(([k, v]) =>
            readOnly ? (
              <ReadOnlyReason key={k}>
                <span className={MONO}>
                  {k} = {Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : String(v)}
                </span>
              </ReadOnlyReason>
            ) : (
              <RuleValueEditor
                key={k}
                ruleId={rule.id}
                name={k}
                value={draftParams[k] ?? v}
                effectivePredicates={toggle?.effective_predicates}
                onChange={(nv) => setDraftParams((d) => ({ ...d, [k]: nv }))}
                onError={(m) =>
                  setParamErrors((e) => {
                    const { [k]: _drop, ...rest } = e;
                    return m ? { ...rest, [k]: m } : rest;
                  })
                }
              />
            ),
          )}
          {firstError && <p role="alert" className="text-[12px] text-st-blocked">{firstError}</p>}
          {!readOnly && paramsDirty && !pending && (
            <Button
              size="sm"
              variant="outline"
              className="self-start"
              onClick={() => ask({ body: { params: cleaned }, what: "Change params" })}
            >
              Change params
            </Button>
          )}
        </div>
      )}

      {pending && (
        <ReasonForm
          what={pending.what}
          reason={reason}
          onReason={setReason}
          saving={saving}
          onSubmit={submit}
          onCancel={() => {
            setPending(null);
            setError(null);
            setDraftParams(structuredClone(params));
          }}
        />
      )}
      {error !== null && <ToggleError err={error} />}

      <RuleHistory ruleId={rule.id} />
    </div>
  );
}

function ReasonForm({
  what,
  reason,
  onReason,
  saving,
  onSubmit,
  onCancel,
}: {
  what: string;
  reason: string;
  onReason: (v: string) => void;
  saving: boolean;
  onSubmit: () => void;
  onCancel: () => void;
}) {
  return (
    <form
      className="flex flex-col gap-1.5"
      data-testid="rules-toggle-reason"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
    >
      <span className="text-[12px] text-fg-2">{what}: why? The reason is the audit record.</span>
      <Input aria-label="Reason" autoFocus value={reason} placeholder="Say why, in a sentence" onChange={(e) => onReason(e.target.value)} />
      <div className="flex gap-2">
        <Button size="sm" type="submit" disabled={!reason.trim() || saving}>
          {saving ? "Saving…" : what}
        </Button>
        <Button size="sm" type="button" variant="outline" disabled={saving} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

/** The server's words, as sent: a lock's 409 with its "see rule", an org plan's 402. */
function ToggleError({ err }: { err: unknown }) {
  return (
    <div role="alert" className="flex flex-col gap-1" data-testid="rules-toggle-error">
      <span className="text-[12px] leading-snug text-st-blocked">{errorDetail(err, "Could not change this rule.")}</span>
      {isRefusal(err) && <RefusalRuleLink ruleId={errorRuleId(err)} />}
    </div>
  );
}

/** Drafts as they will be saved: list entries trimmed with blanks dropped, map rows without a key dropped. */
export function cleanParams(drafts: Params): Params {
  const out: Params = {};
  for (const [k, v] of Object.entries(drafts)) {
    if (Array.isArray(v)) out[k] = v.map((x) => x.trim()).filter(Boolean);
    else if (typeof v === "object" && v !== null) {
      out[k] = Object.fromEntries(Object.entries(v).filter(([mk]) => mk.trim()).map(([mk, ml]) => [mk.trim(), ml]));
    } else out[k] = v;
  }
  return out;
}
