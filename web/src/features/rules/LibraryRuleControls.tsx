/**
 * Library rule toggles on the Rules inspector (GRPH-1268 / PRD-P63 §8, S2c).
 *
 * A library rule row carries an on/off toggle, its params inline and the source of each value
 * (`project` / `org` / `platform default`). Its level is the primitive's, which the server
 * already draws; an off rule stays listed as NOT USED. Every change is a person's and needs a
 * reason, which is the audit record (D10), so the row asks for one before it writes and then
 * says who changed it and when. There is deliberately no bulk "enable all": adopting a rule is
 * one decision per rule. Writes go through `PUT /api/rules/toggles/{id}` and, for an org, the
 * org route — whose 402 on `free`/`pro` is shown as the API sends it.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { RefusalRuleLink } from "@/features/activity/RefusalRuleLink";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { errorDetail, errorRuleId, isRefusal } from "@/lib/errors";
import {
  useGitops,
  useRuleToggleLastChange,
  useRuleToggles,
  useSetOrgRuleToggle,
  useSetRuleToggle,
} from "@/lib/queries";
import { relTime } from "@/lib/relTime";
import type { RuleFlowRule, RuleParamValue, RuleToggleSource, RuleToggleWrite } from "@/lib/types";

import { toggleSourceLabel } from "./levels";
import { useOrgName } from "./RuleEditors";

const MONO = "font-mono";
const note = "text-[11.5px] leading-snug text-muted";

type Params = Record<string, RuleParamValue>;
type Pending = { scope: "project" | "org"; body: Omit<RuleToggleWrite, "reason">; what: string };

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

function SourceChip({ source, testId }: { source: RuleToggleSource | string | null | undefined; testId: string }) {
  const set = source === "project" || source === "org";
  return (
    <span
      data-testid={testId}
      className={cn(MONO, "rounded-[4px] px-1.5 text-meta tracking-[0.04em]")}
      style={{
        border: `1px ${set ? "solid" : "dashed"} ${source === "project" ? "rgba(167,139,250,.42)" : "#626c75"}`,
        color: source === "project" ? "#c9b8ff" : "#dfe3e7",
      }}
    >
      {toggleSourceLabel(source)}
    </span>
  );
}

function ToggleBody({ rule }: { rule: RuleFlowRule }) {
  const { activeId } = useProjectCtx();
  const { data: gitops } = useGitops(activeId);
  const { data: toggles } = useRuleToggles(activeId);
  const toggle = toggles?.toggles.find((t) => t.rule_id === rule.id);
  const syncName = useOrgName(gitops);
  const orgId = gitops?.org_id ?? toggle?.locked_by ?? null;
  const orgName = syncName === "your org" && toggle?.locked_by ? toggle.locked_by : syncName;

  const on = rule.value === true;
  const params: Params = (rule.params?.value as Params | null) ?? {};
  const locked = !!rule.locked;

  const [draftParams, setDraftParams] = React.useState<Record<string, string>>(() => paramDrafts(params));
  const [pending, setPending] = React.useState<Pending | null>(null);
  const [reason, setReason] = React.useState("");
  const [orgOpen, setOrgOpen] = React.useState(false);
  const setProject = useSetRuleToggle(activeId);
  const setOrg = useSetOrgRuleToggle(orgId ?? "");
  const saving = setProject.isPending || setOrg.isPending;
  const [error, setError] = React.useState<unknown>(null);

  // A refetched flow (another person's change, or ours) resets the param inputs to what holds.
  const paramsKey = JSON.stringify(params);
  React.useEffect(() => {
    setDraftParams(paramDrafts(JSON.parse(paramsKey) as Params));
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
    if (pending.scope === "org") setOrg.mutate({ ruleId: rule.id, body }, opts);
    else setProject.mutate({ ruleId: rule.id, body }, opts);
  }

  const parsed = parseParams(params, draftParams);
  const paramsDirty = parsed.ok && JSON.stringify(parsed.value) !== paramsKey;

  return (
    <div className="flex flex-col gap-2 rounded-[8px] border border-[#2a333a] bg-bg p-2.5" data-testid="rules-toggle">
      <div className="flex items-center gap-2">
        <span className={cn(MONO, "text-[10px] text-muted-2")}>{on ? "ON" : "OFF"}</span>
        <span className="flex-1" />
        <SourceChip source={rule.source} testId="rules-toggle-source" />
      </div>

      {locked ? (
        <span className="text-[12px] text-fg-2" data-testid="rules-toggle-locked">
          set by org {orgName} · locked
        </span>
      ) : (
        <button
          type="button"
          role="switch"
          aria-checked={on}
          aria-label={`${rule.title}: ${on ? "on" : "off"}`}
          disabled={saving}
          onClick={() => ask({ scope: "project", body: { on: !on }, what: on ? "Turn off" : "Turn on" })}
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
            <SourceChip source={rule.params?.source} testId="rules-toggle-params-source" />
          </div>
          {Object.entries(params).map(([k, v]) =>
            locked ? (
              <span key={k} className={cn(MONO, "text-[12px] text-fg-2")}>
                {k} = {String(v)}
              </span>
            ) : typeof v === "boolean" ? (
              <label key={k} className={cn(MONO, "flex items-center gap-2 text-[12px] text-fg-2")}>
                <input
                  type="checkbox"
                  aria-label={k}
                  checked={draftParams[k] === "true"}
                  onChange={(e) => setDraftParams({ ...draftParams, [k]: String(e.target.checked) })}
                />
                {k}
              </label>
            ) : (
              <label key={k} className={cn(MONO, "flex items-center gap-2 text-[12px] text-fg-2")}>
                <span className="w-28 flex-none break-all">{k}</span>
                <Input
                  aria-label={k}
                  inputMode={typeof v === "number" ? "numeric" : undefined}
                  value={draftParams[k] ?? ""}
                  onChange={(e) => setDraftParams({ ...draftParams, [k]: e.target.value })}
                />
              </label>
            ),
          )}
          {!parsed.ok && <p role="alert" className="text-[12px] text-st-blocked">{parsed.error}</p>}
          {!locked && paramsDirty && !pending && (
            <Button
              size="sm"
              variant="outline"
              className="self-start"
              onClick={() => parsed.ok && ask({ scope: "project", body: { params: parsed.value }, what: "Change params" })}
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
            setDraftParams(paramDrafts(params));
          }}
        />
      )}
      {error !== null && <ToggleError err={error} />}

      <LastChange ruleId={rule.id} />

      {orgId && (
        <div className="flex flex-col gap-1.5 border-t border-line pt-2">
          {!orgOpen ? (
            <button
              type="button"
              onClick={() => setOrgOpen(true)}
              className="self-start text-[11.5px] text-accent hover:underline"
              data-testid="rules-toggle-org-open"
            >
              Set for every project in {orgName}…
            </button>
          ) : (
            <div className="flex flex-wrap gap-1.5" role="group" aria-label={`${orgName} setting`} data-testid="rules-toggle-org">
              <Button size="sm" variant="outline" disabled={saving} onClick={() => ask({ scope: "org", body: { on: true }, what: `Turn on for ${orgName}` })}>
                On for the org
              </Button>
              <Button size="sm" variant="outline" disabled={saving} onClick={() => ask({ scope: "org", body: { on: false }, what: `Turn off for ${orgName}` })}>
                Off for the org
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={saving}
                onClick={() => ask({ scope: "org", body: { locked: !locked }, what: locked ? `Unlock for ${orgName}` : `Lock for ${orgName}` })}
              >
                {locked ? "Unlock" : "Lock"}
              </Button>
            </div>
          )}
        </div>
      )}
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

function LastChange({ ruleId }: { ruleId: string }) {
  const { activeId } = useProjectCtx();
  const { data: last } = useRuleToggleLastChange(activeId, ruleId);
  if (!last) return null;
  const why = typeof last.meta?.reason === "string" ? last.meta.reason : "";
  return (
    <span className={note} data-testid="rules-toggle-changed">
      Changed by {last.actor_label || last.actor_id} {relTime(last.ts) ?? "at an unknown time"}
      {why && <> — “{why}”</>}
    </span>
  );
}

function paramDrafts(params: Params): Record<string, string> {
  return Object.fromEntries(Object.entries(params).map(([k, v]) => [k, String(v)]));
}

/** Drafts back to the template's own types; the server refuses a wrong type, so say it first. */
function parseParams(
  current: Params,
  drafts: Record<string, string>,
): { ok: true; value: Params } | { ok: false; error: string } {
  const out: Params = {};
  for (const [k, v] of Object.entries(current)) {
    const d = drafts[k] ?? String(v);
    if (typeof v === "number") {
      const n = Number(d.trim());
      const whole = Number.isInteger(v);
      if (d.trim() === "" || !Number.isFinite(n) || (whole && !Number.isInteger(n))) {
        return { ok: false, error: `${k} must be ${whole ? "a whole number" : "a number"}.` };
      }
      out[k] = n;
    } else if (typeof v === "boolean") {
      out[k] = d === "true";
    } else {
      out[k] = d;
    }
  }
  return { ok: true, value: out };
}
