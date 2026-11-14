/**
 * Quick configure (GRPH-1600, GRPH-P82): a project takes a strictness level from the installed
 * rule pack in one reviewed step. The pack ships one profile per level (Exploratory, Standard,
 * Regulated), each versioned. Everything comes from the server:
 *
 * - `GET /rules/profiles` gives the profiles, their pins and the project's applied level;
 * - `GET /rules/profiles/{id}/drift` says how the project has moved from the applied level since;
 * - `GET /rules/profiles/updates` offers a newer version of the applied level;
 * - `POST /rules/profiles/{id}/apply` and `.../updates/accept` write, a person's act with a reason.
 *
 * Screens: the bar above the map (the level chip and any update on offer), the picker (a card per
 * level), the compare table (by stage, the cells that would change outlined, WEAKENING in amber),
 * and the shared review sheet. A dropped rule keeps its value and is recorded as an exception; an
 * org-locked rule is skipped and named. Nothing is written until the sheet is saved. A level never
 * touches repo-specific values: the pack's profiles do not pin them.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import {
  useAcceptRuleProfileUpdate,
  useApplyRuleProfile,
  useRuleProfileDrift,
  useRuleProfileUpdates,
  useRuleProfiles,
} from "@/lib/queries";
import type {
  RuleFlowRule,
  RuleParamValue,
  RuleProfile,
  RuleProfileApplied,
  RuleProfileDriftRow,
  RuleProfilePin,
  RuleProfileUpdate,
  RulesFlow,
} from "@/lib/types";

import { useCanWriteRules } from "./LibraryRuleControls";
import { RuleReviewSheet, showValue, type ParamChange, type RuleChange } from "./RuleReviewSheet";
import { enumFor } from "./RuleValueEditor";

const MONO = "font-mono";
const AMBER = "#ffb454";

export type QuickScreen = { screen: "picker" } | { screen: "compare"; profile: string } | { screen: "update"; profile: string };

// ── Pure reads over the flow and the pins (exported for tests) ──────────────────────────────

export const versionText = (v: string | number | null | undefined) => (v === null || v === undefined ? "" : String(v));

export function ruleIndex(flow: RulesFlow): Map<string, RuleFlowRule> {
  return new Map(flow.stages.flatMap((s) => s.rules).map((r) => [r.id, r]));
}

/** The predicates every deployment requires; an emptied `predicates` list falls back to these. */
const CI_FLOOR: readonly string[] = ["suite_green"];

const isOn = (r: RuleFlowRule) => r.value === true;
const paramsOf = (r: RuleFlowRule) => (r.params?.value ?? {}) as Record<string, RuleParamValue>;
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** The pin's named params that differ from the rule's value now. */
function paramDiffs(pin: RuleProfilePin, rule: RuleFlowRule): string[] {
  const now = paramsOf(rule);
  return Object.keys(pin.params ?? {}).filter((k) => !same(pin.params[k], now[k]));
}

/** True when applying this pin would change the rule (on/off or a named param). */
export function pinChanges(pin: RuleProfilePin, rule: RuleFlowRule | undefined): boolean {
  if (!rule) return false;
  return isOn(rule) !== (pin.state === "on") || paramDiffs(pin, rule).length > 0;
}

/** What the change loosens, in words; undefined when it does not loosen anything. */
export function weakeningOf(pin: RuleProfilePin, rule: RuleFlowRule | undefined): string | undefined {
  if (!rule) return undefined;
  if (isOn(rule) && pin.state === "off") return `WEAKENING: turns ${rule.title} off.`;
  const now = paramsOf(rule);
  for (const k of paramDiffs(pin, rule)) {
    const after = pin.params[k];
    const opt = typeof after === "string" ? enumFor(rule.id, k)?.find((o) => o.value === after) : undefined;
    if (opt?.risky) return `WEAKENING: ${k} becomes ${opt.label}. ${opt.explain}`;
    if (typeof after === "number" && typeof now[k] === "number" && after < (now[k] as number)) {
      return `WEAKENING: ${k} drops from ${now[k]} to ${after}.`;
    }
    if (Array.isArray(after) && Array.isArray(now[k])) {
      // An emptied list follows the deployment, whose floor always holds the CI predicate.
      const kept = after.length ? after : CI_FLOOR;
      const lost = (now[k] as unknown[]).filter((e) => !kept.includes(e as string));
      if (lost.length) return `WEAKENING: ${k} drops ${lost.join(", ")}.`;
    }
  }
  return undefined;
}

/** Rules a level would change here, leaving out the ones it is told to ignore. */
export function differsIn(profile: RuleProfile, rules: Map<string, RuleFlowRule>, ignore: ReadonlySet<string> = new Set()): string[] {
  return profile.pins.filter((p) => !ignore.has(p.rule_id) && pinChanges(p, rules.get(p.rule_id))).map((p) => p.rule_id);
}

export interface LevelPlan {
  changes: RuleChange[];
  /**
   * Every org-locked rule the level pins and the person did not leave out, whether or not it
   * already matches: the server refuses the whole apply on any of them unless `skip_locked`,
   * and records them as skipped. Named in the sheet so that record is no surprise.
   */
  locked: string[];
  /** Pinned rules this server's library cannot enforce: the apply refuses the whole level. */
  unenforceable: string[];
}

export function planFor(profile: RuleProfile, rules: Map<string, RuleFlowRule>, leaveOut: ReadonlySet<string>): LevelPlan {
  const changes: RuleChange[] = [];
  const locked: string[] = [];
  const unenforceable: string[] = [];
  for (const pin of profile.pins) {
    const rule = rules.get(pin.rule_id);
    if (!pin.enforceable || !rule) {
      unenforceable.push(pin.rule_id);
      continue;
    }
    if (leaveOut.has(pin.rule_id)) continue;
    // Locked is checked before "does it change": the server counts a locked pin that already
    // matches too (rules_org.apply_project_profile), so skip_locked must be sent for it.
    if (rule.locked) {
      locked.push(pin.rule_id);
      continue;
    }
    if (!pinChanges(pin, rule)) continue;
    const want = pin.state === "on";
    const now = paramsOf(rule);
    const params: ParamChange[] = paramDiffs(pin, rule).map((k) => ({ name: k, before: now[k], after: pin.params[k] }));
    changes.push({
      kind: "rule",
      ruleId: rule.id,
      title: rule.title,
      on: isOn(rule) !== want ? { before: isOn(rule), after: want } : undefined,
      params,
      sourceBefore: rule.params?.source ?? rule.source ?? "platform default",
      sourceAfter: "project",
      risk: weakeningOf(pin, rule),
    });
  }
  return { changes, locked, unenforceable };
}

function pinText(pin: RuleProfilePin | undefined, names: string[]): string {
  if (!pin) return "not set";
  const ps = names.filter((k) => k in (pin.params ?? {})).map((k) => `${k}: ${showValue(pin.params[k])}`);
  return [pin.state, ...ps].join(" · ");
}

function currentText(rule: RuleFlowRule | undefined, names: string[]): string {
  if (!rule) return "not in this server's library";
  const now = paramsOf(rule);
  return [isOn(rule) ? "on" : "off", ...names.filter((k) => k in now).map((k) => `${k}: ${showValue(now[k])}`)].join(" · ");
}

function levelName(profiles: RuleProfile[], id: string | null | undefined): string {
  return profiles.find((p) => p.id === id)?.title ?? id ?? "";
}

/** "Regulated v0.1.0 · 1 exception · 1 skipped · drifted (2)". */
export function chipText(applied: RuleProfileApplied, title: string, drifted: number | null): string {
  const parts = [`Level: ${title}${applied.version !== null ? ` v${versionText(applied.version)}` : ""}`];
  const ex = applied.exceptions.length;
  if (ex) parts.push(`${ex} exception${ex === 1 ? "" : "s"}`);
  const sk = applied.skipped.length;
  if (sk) parts.push(`${sk} skipped (org-locked)`);
  if (drifted) parts.push(`drifted (${drifted})`);
  return parts.join(" · ");
}

function updateRuleCount(u: RuleProfileUpdate): number | null {
  return u.diff ? new Set(u.diff.map((d) => d.rule_id)).size : null;
}

// ── The bar above the map ───────────────────────────────────────────────────────────────────

export function QuickConfigureBar({ onOpen }: { onOpen: (s: QuickScreen) => void }) {
  const { activeId } = useProjectCtx();
  const profiles = useRuleProfiles(activeId);
  const applied = profiles.data?.applied ?? null;
  const drift = useRuleProfileDrift(activeId, applied?.profile ?? null);
  const updates = useRuleProfileUpdates(activeId);
  if (!profiles.data || !profiles.data.pack_loaded || profiles.data.profiles.length === 0) return null;
  const list = profiles.data.profiles;
  const offer = updates.data?.updates.find((u) => u.profile === applied?.profile) ?? updates.data?.updates[0];
  const drifted = drift.data?.state === "drifted" ? drift.data.differs_in : null;
  return (
    <div className="mx-5 mt-3 flex flex-wrap items-center gap-2" data-testid="rules-quick-bar">
      <span className={cn(MONO, "text-meta font-semibold text-accent")}>QUICK CONFIGURE</span>
      {applied?.profile ? (
        <button
          type="button"
          onClick={() => onOpen({ screen: "compare", profile: applied.profile! })}
          data-testid="rules-level-chip"
          data-drifted={drifted ? "true" : undefined}
          className="h-7 rounded-[8px] border px-2.5 text-small"
          style={{ borderColor: drifted ? AMBER : "#2a333a", color: drifted ? AMBER : undefined }}
          title={drifted ? "Open the compare view on the rules changed by hand" : "Open the compare view"}
        >
          {chipText(applied, levelName(list, applied.profile), drifted)}
        </button>
      ) : (
        <span className="text-small text-muted" data-testid="rules-level-none">No level applied</span>
      )}
      <button
        type="button"
        onClick={() => onOpen({ screen: "picker" })}
        data-testid="rules-open-quick"
        className="h-7 rounded-[8px] border border-control px-2.5 text-small text-fg-2 hover:text-fg"
      >
        Choose a level
      </button>
      {offer && (
        <span className="flex flex-wrap items-center gap-2 text-small text-fg-2" data-testid="rules-level-update">
          A newer {levelName(list, offer.profile)} is available
          {updateRuleCount(offer) !== null && `: ${updateRuleCount(offer)} rule${updateRuleCount(offer) === 1 ? "" : "s"} change`}
          <button
            type="button"
            onClick={() => onOpen({ screen: "update", profile: offer.profile })}
            data-testid="rules-open-update"
            className="h-7 rounded-[8px] border border-accent px-2.5 text-small text-accent"
          >
            Review v{versionText(offer.offered_version)}
          </button>
        </span>
      )}
    </div>
  );
}

// ── The panel: picker, compare, and the review sheet ───────────────────────────────────────

export function QuickConfigure({
  flow,
  narrow,
  orgName,
  start,
  onExit,
  onApplied,
  onSetup,
}: {
  flow: RulesFlow;
  narrow: boolean;
  orgName: string;
  start: QuickScreen;
  onExit: () => void;
  onApplied: (ruleIds: string[]) => void;
  /** Open the setup wizard, where the repo-specific values are set. */
  onSetup: () => void;
}) {
  const { activeId } = useProjectCtx();
  const canWrite = useCanWriteRules(activeId);
  const q = useRuleProfiles(activeId);
  const applied = q.data?.applied ?? null;
  const drift = useRuleProfileDrift(activeId, applied?.profile ?? null);
  const updates = useRuleProfileUpdates(activeId);
  const apply = useApplyRuleProfile(activeId);
  const accept = useAcceptRuleProfileUpdate(activeId);
  const [screen, setScreen] = React.useState<QuickScreen>(start);
  const [reviewing, setReviewing] = React.useState(start.screen === "update");
  const [dropped, setDropped] = React.useState<ReadonlySet<string>>(new Set());
  const [reason, setReason] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const rules = React.useMemo(() => ruleIndex(flow), [flow]);

  const profiles = q.data?.profiles ?? [];
  const order = profiles.map((p) => p.id);
  const selectedId = screen.screen === "picker" ? null : screen.profile;
  const selected = profiles.find((p) => p.id === selectedId) ?? null;
  const isUpdate = screen.screen === "update";
  const offer = isUpdate ? updates.data?.updates.find((u) => u.profile === selectedId) ?? null : null;
  // An update carries the last apply's exceptions (the server does it); the person can't drop more.
  const carried = React.useMemo<ReadonlySet<string>>(
    () => new Set(isUpdate && applied?.profile === selectedId ? applied.exceptions : []),
    [isUpdate, applied, selectedId],
  );
  const leaveOut = isUpdate ? carried : dropped;
  const plan = selected ? planFor(selected, rules, leaveOut) : null;
  const driftRows = new Map<string, RuleProfileDriftRow>(
    applied?.profile && applied.profile === selectedId ? (drift.data?.drift ?? []).map((r) => [r.rule_id, r]) : [],
  );

  function choose(id: string) {
    setDropped(new Set());
    setReason("");
    setError(null);
    setScreen({ screen: "compare", profile: id });
  }

  const version = isUpdate ? versionText(offer?.offered_version ?? selected?.version) : versionText(selected?.version);
  const prefix = selected ? `applying ${selected.title} ${version}:` : "";

  async function save() {
    if (!selected) return;
    setError(null);
    const why = `${prefix} ${reason.trim()}`;
    try {
      const out = isUpdate
        ? await accept.mutateAsync({ profileId: selected.id, reason: why, version: version || null })
        : await apply.mutateAsync({
            profileId: selected.id,
            body: { reason: why, except: [...dropped].sort(), skip_locked: (plan?.locked.length ?? 0) > 0 },
          });
      onApplied(out.applied);
    } catch (err) {
      setError(errorDetail(err, "The server refused the level. Nothing was applied."));
    }
  }

  const saving = apply.isPending || accept.isPending;
  const lockedFooter = plan && plan.locked.length > 0 && (
    <p className="text-small leading-snug text-fg-2" data-testid="rules-quick-skipped">
      Skipped, set by org {orgName} · locked: {plan.locked.map((id) => rules.get(id)?.title ?? id).join(", ")}. The level applies to the rest.
    </p>
  );
  const droppedFooter = !isUpdate && dropped.size > 0 && (
    <div className="flex flex-col gap-1" data-testid="rules-quick-dropped">
      <span className="text-small text-fg-2">
        Recorded as {selected?.title}, except: {[...dropped].map((id) => rules.get(id)?.title ?? id).join(", ")}.
      </span>
      <div className="flex flex-wrap gap-1.5">
        {[...dropped].map((id) => (
          <button
            key={id}
            type="button"
            onClick={() => setDropped((d) => new Set([...d].filter((x) => x !== id)))}
            className="h-6 rounded-[6px] border border-control px-2 text-small text-fg-2"
          >
            Restore {rules.get(id)?.title ?? id}
          </button>
        ))}
      </div>
    </div>
  );
  const carriedFooter = isUpdate && carried.size > 0 && (
    <p className="text-small text-fg-2" data-testid="rules-quick-carried">
      Kept as exceptions from your last apply: {[...carried].map((id) => rules.get(id)?.title ?? id).join(", ")}.
    </p>
  );

  return (
    <div className="absolute inset-0 z-20 overflow-y-auto bg-bg" data-testid="rules-quick-configure" role="region" aria-label="Quick configure">
      <div className={cn("mx-auto flex flex-col gap-4 p-5", narrow ? "max-w-full" : "max-w-[1040px]")}>
        <div className="flex items-center gap-2">
          <span className={cn(MONO, "text-meta font-semibold text-accent")}>QUICK CONFIGURE</span>
          {q.data?.pack_version && <span className={cn(MONO, "text-small text-muted")}>rule pack {q.data.pack_version}</span>}
          <span className="flex-1" />
          {screen.screen !== "picker" && (
            <Button size="sm" variant="outline" onClick={() => setScreen({ screen: "picker" })} data-testid="rules-quick-levels">
              All levels
            </Button>
          )}
          <Button size="sm" variant="outline" onClick={onExit} data-testid="rules-quick-exit">
            Close
          </Button>
        </div>

        {q.isError ? (
          <p role="alert" className="text-body text-st-blocked">{errorDetail(q.error, "The profiles could not be read.")}</p>
        ) : !q.data ? (
          <p className="text-body text-muted">Reading the rule pack…</p>
        ) : !q.data.pack_loaded || profiles.length === 0 ? (
          <p className="text-body text-fg-2" data-testid="rules-quick-no-pack">
            No rule pack is loaded on this server, so there are no levels to choose from.
          </p>
        ) : screen.screen === "picker" ? (
          <Picker profiles={profiles} order={order} rules={rules} applied={applied} drifted={drift.data?.state === "drifted" ? drift.data.differs_in : null} narrow={narrow} onChoose={choose} />
        ) : selected && plan ? (
          <Compare
            profiles={profiles}
            selected={selected}
            rules={rules}
            flow={flow}
            narrow={narrow}
            orgName={orgName}
            plan={plan}
            dropped={leaveOut}
            driftRows={driftRows}
            onSelect={choose}
            onSetup={onSetup}
            onReview={() => setReviewing(true)}
            isUpdate={isUpdate}
          />
        ) : (
          <p className="text-body text-fg-2">The pack has no level {selectedId}.</p>
        )}
      </div>
      {reviewing && selected && plan && plan.unenforceable.length === 0 && (
        <RuleReviewSheet
          changes={plan.changes}
          reason={reason}
          onReason={setReason}
          saving={saving}
          error={error ?? (canWrite === false ? "You don't have a role that can change rules." : null)}
          narrow={narrow}
          onSave={() => void save()}
          onBack={() => setReviewing(false)}
          reasonPrefix={prefix}
          onDrop={isUpdate ? undefined : (id) => setDropped((d) => new Set([...d, id]))}
          footer={
            <>
              {isUpdate && (
                <p className="text-small text-fg-2" data-testid="rules-quick-update-note">
                  {selected.title} v{versionText(offer?.applied_version ?? applied?.version)} → v{version}. Nothing changes until you accept.
                </p>
              )}
              {plan.changes.length === 0 && (
                <p className="text-small text-fg-2" data-testid="rules-quick-nothing">
                  No rule changes value here; applying records the level.
                </p>
              )}
              {droppedFooter}
              {carriedFooter}
              {lockedFooter}
            </>
          }
        />
      )}
    </div>
  );
}

function StrictnessBar({ at, of }: { at: number; of: number }) {
  return (
    <span className="flex gap-0.5" aria-label={`Strictness ${at + 1} of ${of}`} role="img">
      {Array.from({ length: of }, (_, i) => (
        <span key={i} className="h-1.5 w-5 rounded-full" style={{ background: i <= at ? "#5ad1a0" : "#2a333a" }} />
      ))}
    </span>
  );
}

function Picker({
  profiles,
  order,
  rules,
  applied,
  drifted,
  narrow,
  onChoose,
}: {
  profiles: RuleProfile[];
  order: string[];
  rules: Map<string, RuleFlowRule>;
  applied: RuleProfileApplied | null;
  drifted: number | null;
  narrow: boolean;
  onChoose: (id: string) => void;
}) {
  const intended = new Set([...(applied?.exceptions ?? []), ...(applied?.skipped ?? [])]);
  const counts = profiles.map((p) => ({ id: p.id, n: differsIn(p, rules, p.id === applied?.profile ? intended : new Set()).length }));
  const appliedClean = !!applied?.profile && !drifted && counts.find((c) => c.id === applied.profile)?.n === 0;
  const current = appliedClean ? applied!.profile : counts.find((c) => c.n === 0)?.id ?? null;
  const nearest = [...counts].sort((a, b) => a.n - b.n)[0];
  return (
    <section className="flex flex-col gap-3" aria-label="Choose a level" data-testid="rules-quick-picker">
      <h2 className="text-lead font-semibold">Choose a strictness level</h2>
      {!current && nearest && (
        <p className="text-body text-fg-2" data-testid="rules-quick-custom">
          Custom: matches none. Nearest is {levelName(profiles, nearest.id)}, which differs in {nearest.n} rule{nearest.n === 1 ? "" : "s"}.
        </p>
      )}
      <div className={cn("flex gap-3", narrow ? "flex-col" : "flex-row")}>
        {profiles.map((p) => {
          const at = order.indexOf(p.id);
          const isCurrent = current === p.id;
          const n = counts.find((c) => c.id === p.id)?.n ?? 0;
          return (
            <div
              key={p.id}
              data-testid={`rules-quick-card-${p.id}`}
              data-current={isCurrent ? "true" : undefined}
              className="flex flex-1 flex-col gap-2 rounded-[12px] border p-3.5"
              style={{ borderColor: isCurrent ? "#5ad1a0" : "#2a333a" }}
            >
              <div className="flex items-center gap-2">
                <span className="text-lead font-semibold">{p.title}</span>
                <span className={cn(MONO, "text-small text-muted")}>v{versionText(p.version)}</span>
                <span className="flex-1" />
                {isCurrent && <span className={cn(MONO, "text-meta font-semibold text-[#5ad1a0]")}>CURRENT LEVEL</span>}
              </div>
              <StrictnessBar at={at} of={order.length} />
              {p.summary && <span className="text-body leading-snug text-fg-2">{p.summary}</span>}
              <span className="text-small text-muted">
                {p.pins.filter((x) => x.state === "on").length} rules on · {p.pins.length} set
                {!isCurrent && ` · ${n} would change here`}
              </span>
              <Button size="sm" variant={isCurrent ? "outline" : "default"} onClick={() => onChoose(p.id)} data-testid={`rules-quick-choose-${p.id}`}>
                Compare {p.title}
              </Button>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function Compare({
  profiles,
  selected,
  rules,
  flow,
  narrow,
  orgName,
  plan,
  dropped,
  driftRows,
  onSelect,
  onSetup,
  onReview,
  isUpdate,
}: {
  profiles: RuleProfile[];
  selected: RuleProfile;
  rules: Map<string, RuleFlowRule>;
  flow: RulesFlow;
  narrow: boolean;
  orgName: string;
  plan: LevelPlan;
  dropped: ReadonlySet<string>;
  driftRows: Map<string, RuleProfileDriftRow>;
  onSelect: (id: string) => void;
  onSetup: () => void;
  onReview: () => void;
  isUpdate: boolean;
}) {
  const pinned = new Map<string, Map<string, RuleProfilePin>>();
  for (const p of profiles) for (const pin of p.pins) {
    if (!pinned.has(pin.rule_id)) pinned.set(pin.rule_id, new Map());
    pinned.get(pin.rule_id)!.set(p.id, pin);
  }
  const stageOrder = flow.stages.map((s) => s.id);
  const stageOf = (id: string) => rules.get(id)?.stage ?? id.split(".")[0];
  const ids = [...pinned.keys()].sort((a, b) => {
    const d = stageOrder.indexOf(stageOf(a)) - stageOrder.indexOf(stageOf(b));
    return d !== 0 ? d : a.localeCompare(b);
  });
  const groups: { stage: string; title: string; ids: string[] }[] = [];
  for (const id of ids) {
    const st = stageOf(id);
    let g = groups.find((x) => x.stage === st);
    if (!g) groups.push((g = { stage: st, title: flow.stages.find((s) => s.id === st)?.title ?? st, ids: [] }));
    g.ids.push(id);
  }
  const names = (id: string) => [...new Set([...pinned.get(id)!.values()].flatMap((p) => Object.keys(p.params ?? {})))];
  const changing = new Set(plan.changes.map((c) => c.ruleId));

  function marks(id: string) {
    const rule = rules.get(id);
    const pin = pinned.get(id)!.get(selected.id);
    const out: React.ReactNode[] = [];
    const drift = driftRows.get(id);
    if (drift) {
      out.push(
        <span key="d" className={cn(MONO, "text-meta font-semibold")} style={{ color: AMBER }} data-testid={`rules-quick-drift-${id}`} title={drift.detail}>
          {drift.kind === "weakening" ? "CHANGED BY HAND · WEAKENING" : "CHANGED BY HAND"}
        </span>,
      );
    }
    if (rule?.locked && pin && !dropped.has(id)) {
      out.push(
        <span key="l" className="text-small text-muted" data-testid={`rules-quick-locked-${id}`}>
          set by org {orgName} · locked
        </span>,
      );
    } else if (dropped.has(id)) {
      out.push(<span key="x" className="text-small text-muted">kept: exception</span>);
    } else if (pin && changing.has(id) && weakeningOf(pin, rule)) {
      out.push(
        <span key="w" className={cn(MONO, "text-meta font-semibold")} style={{ color: AMBER }} data-testid={`rules-quick-weakening-${id}`}>
          WEAKENING
        </span>,
      );
    }
    return out;
  }

  const outline = (id: string) => changing.has(id);
  return (
    <section className="flex flex-col gap-3" aria-label={`Compare ${selected.title}`} data-testid="rules-quick-compare">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-lead font-semibold">
          {isUpdate ? `Update ${selected.title} to v${versionText(selected.version)}` : `Compare ${selected.title} v${versionText(selected.version)}`}
        </h2>
        <span className="flex-1" />
        {!isUpdate &&
          profiles.map((p) => (
            <button
              key={p.id}
              type="button"
              aria-pressed={p.id === selected.id}
              onClick={() => onSelect(p.id)}
              className={cn("h-7 rounded-[8px] border px-2.5 text-small", p.id === selected.id ? "border-accent text-accent" : "border-control text-fg-2")}
            >
              {p.title}
            </button>
          ))}
      </div>
      <p className="text-body text-fg-2" data-testid="rules-quick-summary">
        {plan.changes.length} rule{plan.changes.length === 1 ? "" : "s"} would change
        {plan.locked.length > 0 && `, ${plan.locked.length} skipped (org-locked)`}. Outlined cells are the ones that would change.
      </p>
      {plan.unenforceable.length > 0 && (
        <p role="alert" className="text-body text-st-blocked" data-testid="rules-quick-unenforceable">
          This server can't enforce {plan.unenforceable.join(", ")}, so it refuses {selected.title} whole. Nothing would be applied.
        </p>
      )}
      {narrow ? (
        <div className="flex flex-col gap-3">
          {groups.map((g) => (
            <div key={g.stage} className="flex flex-col gap-2">
              <span className={cn(MONO, "text-meta font-semibold text-muted")}>{g.title.toUpperCase()}</span>
              {g.ids.map((id) => (
                <div
                  key={id}
                  data-testid={`rules-quick-row-${id}`}
                  data-changes={outline(id) ? "true" : undefined}
                  className="flex flex-col gap-1 rounded-[10px] border p-2.5"
                  style={{ borderColor: outline(id) ? "#5ad1a0" : "#2a333a" }}
                >
                  <span className="text-body font-semibold">{rules.get(id)?.title ?? id}</span>
                  <span className={cn(MONO, "text-small text-fg-2")}>Now: {currentText(rules.get(id), names(id))}</span>
                  {profiles.map((p) => (
                    <span key={p.id} className={cn(MONO, "text-small", p.id === selected.id ? "text-fg" : "text-muted")}>
                      {p.title}: {pinText(pinned.get(id)!.get(p.id), names(id))}
                    </span>
                  ))}
                  <span className="flex flex-wrap gap-2">{marks(id)}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      ) : (
        <table className="w-full border-separate border-spacing-y-1 text-small" data-testid="rules-quick-table">
          <thead>
            <tr className="text-left text-muted">
              <th className="px-2 font-normal">Rule</th>
              <th className="px-2 font-normal">This project</th>
              {profiles.map((p) => (
                <th key={p.id} className={cn("px-2 font-normal", p.id === selected.id && "text-fg")}>
                  {p.title} v{versionText(p.version)}
                </th>
              ))}
              <th className="px-2 font-normal" />
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => (
              <React.Fragment key={g.stage}>
                <tr>
                  <td colSpan={profiles.length + 3} className={cn(MONO, "px-2 pt-2 text-meta font-semibold text-muted")}>
                    {g.title.toUpperCase()}
                  </td>
                </tr>
                {g.ids.map((id) => (
                  <tr key={id} data-testid={`rules-quick-row-${id}`} data-changes={outline(id) ? "true" : undefined}>
                    <td className="px-2 align-top">
                      <span className="text-body">{rules.get(id)?.title ?? id}</span>
                      <span className={cn(MONO, "block text-micro text-muted")}>{id}</span>
                    </td>
                    <td className={cn(MONO, "px-2 align-top text-fg-2")}>{currentText(rules.get(id), names(id))}</td>
                    {profiles.map((p) => (
                      <td
                        key={p.id}
                        className={cn(MONO, "rounded-[6px] px-2 align-top", p.id === selected.id ? "text-fg" : "text-muted")}
                        style={p.id === selected.id && outline(id) ? { outline: "1.5px solid #5ad1a0", outlineOffset: -1 } : undefined}
                        data-testid={p.id === selected.id && outline(id) ? `rules-quick-cell-change-${id}` : undefined}
                      >
                        {pinText(pinned.get(id)!.get(p.id), names(id))}
                      </td>
                    ))}
                    <td className="px-2 align-top">
                      <span className="flex flex-col gap-0.5">{marks(id)}</span>
                    </td>
                  </tr>
                ))}
              </React.Fragment>
            ))}
          </tbody>
        </table>
      )}
      <p className="text-small text-muted" data-testid="rules-quick-repo-note">
        These depend on your repository and no level sets them: exclusive areas, generated files, path classes and engines.{" "}
        <button type="button" onClick={onSetup} className="text-accent hover:underline">
          Set them in Set up rules
        </button>{" "}
        or in each stage.
      </p>
      <div className="flex gap-2">
        <Button size="sm" onClick={onReview} disabled={plan.unenforceable.length > 0} data-testid="rules-quick-review">
          {isUpdate ? `Review the update` : `Review ${selected.title}`}
        </Button>
      </div>
    </section>
  );
}
