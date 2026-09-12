import * as React from "react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { errorDetail } from "@/lib/errors";
import { FLEET_AXES } from "@/lib/types";
import type { FleetPolicy, FleetProfile } from "@/lib/types";

function formatMix(mix: Record<string, number> | null | undefined): string {
  if (!mix) return "";
  return Object.entries(mix)
    .map(([name, share]) => `${name}:${Number(share.toPrecision(4))}`)
    .join(", ");
}

function parseMix(raw: string): Record<string, number> | null | "invalid" {
  const text = raw.trim();
  if (!text) return null;
  const out: Record<string, number> = {};
  for (const part of text.split(",")) {
    const token = part.trim();
    if (!token) continue;
    const m = token.match(/^(\S+)\s*[:=]\s*([0-9]*\.?[0-9]+)$/)
      || token.match(/^(\S+)\s+([0-9]*\.?[0-9]+)$/);
    if (!m) return "invalid";
    out[m[1]] = Number(m[2]);
  }
  return Object.keys(out).length ? out : null;
}

function Section({ title, desc, children }: { title: string; desc: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="mb-7">
      <h2 className="text-[14px] font-semibold tracking-tight">{title}</h2>
      <p className="mb-3 mt-0.5 text-[12px] text-muted">{desc}</p>
      {children}
    </section>
  );
}

/**
 * PRD-37: what the supervisor weighs when a tier has no `--tier` flag. Two boxes, two
 * owners. PREFERENCES are the signed-in user's — an ordered allowlist of harnesses and
 * weights over four axes — and apply wherever their API key runs a supervisor, with a
 * per-project override. POLICY is the project's and is a FILTER: a rule taste cannot outvote.
 *
 * Nothing here chooses a harness. The matrix of what is verified lives in the supervisor's
 * repository and the machine decides what is installed; this view records taste and rules,
 * and the spawn reply explains what they did.
 */
export function Preferences({ projectId, scope, profile, policy, onSaved }: {
  projectId: string; scope: string; profile: FleetProfile | null; policy: FleetPolicy | null;
  onSaved: () => void;
}) {
  const [defaults, setDefaults] = React.useState("");
  const [excludes, setExcludes] = React.useState("");
  const [weights, setWeights] = React.useState<Record<string, number>>({});
  const [budgetTokens, setBudgetTokens] = React.useState("");
  const [mix, setMix] = React.useState("");
  const [overrideHere, setOverrideHere] = React.useState(false);
  const [localOnly, setLocalOnly] = React.useState(false);
  const [crossVendor, setCrossVendor] = React.useState(false);
  const [allowed, setAllowed] = React.useState("");
  const [perItemTokens, setPerItemTokens] = React.useState("");
  const [perAttemptTokens, setPerAttemptTokens] = React.useState("");
  const [perPeriodTokens, setPerPeriodTokens] = React.useState("");
  const [period, setPeriod] = React.useState<"day" | "week" | "month" | "">("");
  const [note, setNote] = React.useState("");
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    setDefaults((profile?.defaults ?? []).join(", "));
    setExcludes((profile?.excludes ?? []).join(", "));
    setWeights({ ...(profile?.weights ?? {}) });
    setBudgetTokens(profile?.budget_tokens != null ? String(profile.budget_tokens) : "");
    setMix(formatMix(profile?.mix));
    setOverrideHere(profile?.scope === "project");
  }, [profile]);
  React.useEffect(() => {
    setLocalOnly(policy?.local_only ?? false);
    setCrossVendor(policy?.reviewer_cross_vendor ?? false);
    setAllowed((policy?.allowed_harnesses ?? []).join(", "));
    setPerItemTokens(policy?.caps?.per_item_tokens != null ? String(policy.caps.per_item_tokens) : "");
    setPerAttemptTokens(policy?.caps?.per_attempt_tokens != null ? String(policy.caps.per_attempt_tokens) : "");
    setPerPeriodTokens(policy?.caps?.per_period_tokens != null ? String(policy.caps.per_period_tokens) : "");
    setPeriod(policy?.caps?.period ?? "");
  }, [policy]);

  const names = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

  async function saveProfile() {
    setError(""); setNote("");
    const parsed = parseMix(mix);
    if (parsed === "invalid") {
      setError("mix is harness:share, e.g. claude:0.4, grok:0.4");
      return;
    }
    try {
      const saved = await api.saveFleetProfile({
        project_id: overrideHere ? projectId : null,
        defaults: names(defaults), excludes: names(excludes), weights,
        budget_tokens: budgetTokens === "" ? null : Number(budgetTokens),
        mix: parsed,
      });
      setNote(saved.scope === "project"
        ? `Saved for ${scope} only. Your default profile still applies elsewhere.`
        : "Saved as your default. It applies in every project without an override.");
      onSaved();
    } catch (e) {
      setError(errorDetail(e, "could not save the profile"));
    }
  }

  async function clearOverride() {
    setError(""); setNote("");
    try {
      const out = await api.clearFleetProfile(projectId);
      setNote(out.cleared ? `Override for ${scope} removed; your default applies here again.`
                          : "There was no override here to remove.");
      onSaved();
    } catch (e) {
      setError(errorDetail(e, "could not clear the override"));
    }
  }

  async function savePolicy() {
    setError(""); setNote("");
    try {
      const caps: { per_item_tokens?: number; per_attempt_tokens?: number; per_period_tokens?: number; period?: "day" | "week" | "month" } = {};
      if (perItemTokens !== "") caps.per_item_tokens = Number(perItemTokens);
      if (perAttemptTokens !== "") caps.per_attempt_tokens = Number(perAttemptTokens);
      if (perPeriodTokens !== "") {
        caps.per_period_tokens = Number(perPeriodTokens);
        // D20 trap: per_period_tokens without a period is a number without a denominator.
        // The server refuses ProfileInvalid; the form must not let you get that far.
        if (!period) {
          setError("per_period_tokens needs a period (day, week, or month)");
          return;
        }
        caps.period = period;
      }
      const out = await api.saveFleetPolicy({
        project_id: projectId, local_only: localOnly, reviewer_cross_vendor: crossVendor,
        allowed_harnesses: names(allowed),
        ...(Object.keys(caps).length ? { caps } : {}),
      });
      setNote(out.policy ? `Policy saved for ${scope}.` : `Policy cleared for ${scope}: no constraint.`);
      onSaved();
    } catch (e) {
      setError(errorDetail(e, "could not save the policy"));
    }
  }

  const field = "h-8 w-full rounded-md border border-line-2 bg-transparent px-2 text-[12.5px]";
  return (
    <Section
      title="Harness preferences"
      desc={
        <>
          What a supervisor weighs when a tier has no <code>--tier</code> flag (PRD-37). Preferences
          are yours and travel with your API key; policy is the project&apos;s and is a filter a
          preference cannot outvote. The spawn reply explains what each one did.
        </>
      }
    >
      <div className="grid gap-4 md:grid-cols-2">
        <div className="rounded-[11px] border border-line-2 p-3" data-testid="fleet-profile">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[12.5px] font-medium">Your profile</span>
            <span className="text-[11px] text-muted" data-testid="fleet-profile-scope">
              {profile ? (profile.scope === "project" ? `override for ${scope}` : "your default")
                       : "none recorded — matrix order and policy alone"}
            </span>
          </div>
          <label className="block text-[11.5px] text-muted">
            Harnesses to consider, in order (empty = all)
            <input className={field} aria-label="Default harnesses" value={defaults}
                   onChange={(e) => setDefaults(e.target.value)} placeholder="gbagent, claude" />
          </label>
          <label className="mt-2 block text-[11.5px] text-muted">
            Never use
            <input className={field} aria-label="Excluded harnesses" value={excludes}
                   onChange={(e) => setExcludes(e.target.value)} placeholder="grok, gbagent:qwen3-coder:30b" />
          </label>
          <div className="mt-2 grid grid-cols-4 gap-2">
            {FLEET_AXES.map((axis) => (
              <label key={axis} className="block text-[11.5px] capitalize text-muted">
                {axis}
                <input className={field} type="number" min={0} max={1} step={0.05}
                       aria-label={`Weight ${axis}`}
                       value={weights[axis] ?? ""}
                       onChange={(e) => {
                         const v = e.target.value;
                         setWeights((w) => {
                           const next = { ...w };
                           if (v === "") delete next[axis]; else next[axis] = Number(v);
                           return next;
                         });
                       }} />
              </label>
            ))}
          </div>
          <label className="mt-2 block text-[11.5px] text-muted">
            Budget tokens per sign-off (soft target; empty = rank-scale cost)
            <input className={field} type="number" min={1} step={1000}
                   aria-label="Budget tokens"
                   value={budgetTokens}
                   onChange={(e) => setBudgetTokens(e.target.value)}
                   placeholder="50000" />
          </label>
          <label className="mt-2 block text-[11.5px] text-muted">
            Mix of recent spawns (empty = always pick the winner)
            <input className={field} aria-label="Mix shares"
                   value={mix}
                   onChange={(e) => setMix(e.target.value)}
                   placeholder="claude:0.4, grok:0.4, gbagent:0.2" />
          </label>
          <p className="mt-1 text-[11px] text-muted">
            Weights are 0–1 and normalised; blank or 0 means indifferent, not excluded. Measured
            axes (quality, latency) count only once five attempts exist. A budget target scores
            rows at or under it 1.0 on cost and does not remove them. A mix is a share of the
            last 20 launches in this project, not a cap, and does not apply until three
            matrix-resolved launches exist.
          </p>
          <label className="mt-2 flex items-center gap-2 text-[12px]">
            <input type="checkbox" checked={overrideHere} onChange={(e) => setOverrideHere(e.target.checked)} />
            Save for {scope} only (override my default here)
          </label>
          <div className="mt-2 flex gap-2">
            <Button size="sm" onClick={() => { void saveProfile(); }}>Save profile</Button>
            {profile?.scope === "project" && (
              <Button size="sm" variant="ghost" onClick={() => { void clearOverride(); }}>Remove override</Button>
            )}
          </div>
        </div>

        <div className="rounded-[11px] border border-line-2 p-3" data-testid="fleet-policy">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[12.5px] font-medium">Project policy</span>
            <span className="text-[11px] text-muted" data-testid="fleet-policy-state">
              {policy ? "constraints on" : "no constraint"}
            </span>
          </div>
          <label className="flex items-center gap-2 text-[12px]">
            <input type="checkbox" checked={localOnly} onChange={(e) => setLocalOnly(e.target.checked)} />
            Local only — no cloud harness or model
          </label>
          <label className="mt-1 flex items-center gap-2 text-[12px]">
            <input type="checkbox" checked={crossVendor} onChange={(e) => setCrossVendor(e.target.checked)} />
            Reviewer from a different vendor than the builder
          </label>
          <label className="mt-2 block text-[11.5px] text-muted">
            Allowed harnesses (empty = any)
            <input className={field} aria-label="Allowed harnesses" value={allowed}
                   onChange={(e) => setAllowed(e.target.value)} placeholder="gbagent, claude" />
          </label>
          <label className="mt-2 block text-[11.5px] text-muted">
            Cap tokens per item (hard filter; empty = none)
            <input className={field} type="number" min={1} step={1000}
                   aria-label="Per-item token cap"
                   value={perItemTokens}
                   onChange={(e) => setPerItemTokens(e.target.value)}
                   placeholder="120000" />
          </label>
          <label className="mt-2 block text-[11.5px] text-muted">
            Cap tokens per attempt (hard filter; empty = none)
            <input className={field} type="number" min={1} step={1000}
                   aria-label="Per-attempt token cap"
                   value={perAttemptTokens}
                   onChange={(e) => setPerAttemptTokens(e.target.value)}
                   placeholder="80000" />
          </label>
          <label className="mt-2 block text-[11.5px] text-muted">
            Cap tokens per period (hard filter; empty = none)
            <input className={field} type="number" min={1} step={1000}
                   aria-label="Per-period token cap"
                   value={perPeriodTokens}
                   onChange={(e) => setPerPeriodTokens(e.target.value)}
                   placeholder="500000" />
          </label>
          <label className="mt-2 block text-[11.5px] text-muted">
            Period for the per-period cap
            <select
              className={field}
              aria-label="Period"
              value={period}
              onChange={(e) => setPeriod(e.target.value as "day" | "week" | "month" | "")}
              data-testid="fleet-policy-period"
            >
              <option value="">— pick one —</option>
              <option value="day">day</option>
              <option value="week">week</option>
              <option value="month">month</option>
            </select>
          </label>
          <p className="mt-1 text-[11px] text-muted">
            A constraint removes rows before anything is scored. Saving with everything off
            stores no policy at all. A cap drops a row that does not report tokens.
            A per-period cap needs a period — <span className="font-mono">per_period_tokens</span> without
            one is <span className="font-mono">ProfileInvalid</span>.
          </p>
          <div className="mt-2">
            <Button size="sm" onClick={() => { void savePolicy(); }}>Save policy</Button>
          </div>
        </div>
      </div>
      {note && <p className="mt-2 text-[12px] text-muted" role="status">{note}</p>}
      {error && <p className="mt-2 text-[12px] text-red-500" role="alert">{error}</p>}
    </Section>
  );
}
