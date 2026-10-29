/**
 * Inline editing on the Rules inspector (GRPH-1163, frames 3a1–3f and 2e).
 *
 * Gitops rows edit through `PATCH /api/projects/{id}/gitops` and only when
 * `control.writable` is true — otherwise read-only text, never a greyed input. `GitopsPanel`
 * is untouched; this wrapper re-uses its exported vocabulary. The budget cap writes the whole
 * fleet policy back with only `caps.per_item_tokens` changed.
 */
import * as React from "react";
import { Link } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  GITOPS_BASE_CHIPS,
  GITOPS_NAMING_TOKENS,
  RELEASE_DEFINED_PLACEHOLDER,
  UNMEASURED_PLACEHOLDER,
} from "@/features/settings/GitopsPanel";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { useFleetPolicy, useGitops, useSaveFleetPolicy, useUpdateGitops } from "@/lib/queries";
import { adminPath } from "@/lib/routes";
import type { FleetPolicy, GitopsPatch, GitopsView, RuleFlowRule } from "@/lib/types";

const MONO = "font-mono";

type FieldKey = Exclude<keyof GitopsPatch, "model">;

/** Rule id → the PATCH key it edits. `gitops.model` is a preset that rewrites other fields, so
 *  it stays in Settings → Gitops rather than becoming a single-row editor here. */
export const GITOPS_RULE_FIELDS: Record<string, FieldKey> = {
  "gitops.base_branch": "base_branch",
  "gitops.no_push_to_base": "no_push_to_base",
  "gitops.branch_name_pattern": "branch_name_pattern",
  "gitops.pr_title_pattern": "pr_title_pattern",
  "gitops.reviewer_bar": "reviewer_bar",
  "release.version_from": "version_from",
  "release.release_defined_in": "release_defined_in",
};

const SELECTS: Partial<Record<FieldKey, readonly (readonly [string, string])[]>> = {
  no_push_to_base: [["", "Unmeasured"], ["true", "Yes"], ["false", "No"]],
  reviewer_bar: [
    ["", "Unmeasured"], ["sign_off", "Graphban sign_off"], ["forge", "Forge approvals"], ["both", "Both"],
  ],
  version_from: [["", "Unmeasured"], ["git_tag", "git tag"], ["semver", "semver"], ["calver", "calver"]],
};

const PATTERN_FIELDS: FieldKey[] = ["branch_name_pattern", "pr_title_pattern"];

const inputClass =
  "h-9 w-full rounded-[9px] border border-control bg-surface-2 px-3 text-[13px] text-fg outline-none";

function currentValue(view: GitopsView, key: FieldKey): string {
  const v =
    key === "version_from" ? view.version_from.value
    : key === "release_defined_in" ? view.release_defined_in.value
    : view.fields[key].value;
  return v === null || v === undefined ? "" : String(v);
}

function toPatch(key: FieldKey, draft: string): GitopsPatch {
  if (draft === "") return { [key]: null };
  return { [key]: key === "no_push_to_base" ? draft === "true" : draft };
}

/** Read-only copy follows who may write: an org project needs an org owner or admin. */
export function readOnlyCopy(view: GitopsView): string {
  return view.org_id ? "Only an org owner or admin can change this" : "You don't have write access to this project";
}

const note = "text-[11.5px] leading-snug text-muted";

/** Replaces the plain value box of a gitops row; `fallback` is that box, used until the view loads. */
export function GitopsRuleEditor({
  rule,
  field,
  fallback,
}: {
  rule: RuleFlowRule;
  field: FieldKey;
  fallback: React.ReactNode;
}) {
  const { activeId } = useProjectCtx();
  const { data: view } = useGitops(activeId);
  if (!view) return <>{fallback}</>;

  const { state, message } = view.control;
  if (state === "linked_unreachable") {
    return (
      <div className="flex flex-col gap-1" data-testid="rules-gitops-unreachable">
        <span className={cn(MONO, "text-[10px] font-semibold tracking-[0.06em] text-[#ff6b6b]")}>UNMEASURED</span>
        <span className={note}>{message || "The org's gitops could not be reached, so no value is shown."}</span>
      </div>
    );
  }
  if (state === "linked_set" || state === "linked_unset") {
    return (
      <div className="flex flex-col gap-1" data-testid="rules-gitops-linked">
        <span className="text-[12px] text-fg-2">
          {state === "linked_set" ? `Inherited from ${view.org_id ?? "your org"}` : "Your org has not set this"}
        </span>
        <Link to={adminPath("gitops")} className="text-[11.5px] text-accent hover:underline">
          Admin → Gitops
        </Link>
      </div>
    );
  }
  if (!view.control.writable) {
    return (
      <div className="flex flex-col gap-1.5">
        {fallback}
        <span className={note} data-testid="rules-gitops-readonly">{readOnlyCopy(view)}</span>
      </div>
    );
  }
  return <GitopsFieldForm key={rule.id} projectId={activeId} view={view} field={field} fallback={fallback} />;
}

function GitopsFieldForm({
  projectId,
  view,
  field,
  fallback,
}: {
  projectId: string;
  view: GitopsView;
  field: FieldKey;
  fallback: React.ReactNode;
}) {
  const saved = currentValue(view, field);
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState(saved);
  const [error, setError] = React.useState("");
  const [done, setDone] = React.useState(false);
  const save = useUpdateGitops(projectId);
  const options = SELECTS[field];
  const dirty = draft !== saved;

  if (!editing) {
    return (
      <div className="flex flex-col gap-1.5">
        {fallback}
        {done && (
          <span className={note} data-testid="rules-gitops-saved">
            Saved. Told to agents in every get_context. Nothing refuses a violation.
          </span>
        )}
        <Button
          size="sm"
          variant="outline"
          className="self-start"
          onClick={() => {
            setDraft(saved);
            setError("");
            setDone(false);
            setEditing(true);
          }}
        >
          Edit
        </Button>
      </div>
    );
  }

  function onSave() {
    save.mutate(toPatch(field, draft), {
      onSuccess: () => {
        setEditing(false);
        setDone(true);
        setError("");
      },
      // The server's 422 is shown as sent and the typed value stays in the input.
      onError: (err) => setError(errorDetail(err, "Could not save gitops.")),
    });
  }

  return (
    <div className="flex flex-col gap-2" data-testid="rules-gitops-editor">
      {options ? (
        <select aria-label={field} className={inputClass} value={draft} onChange={(e) => setDraft(e.target.value)}>
          {options.map(([v, label]) => (
            <option key={v || "unmeasured"} value={v}>{label}</option>
          ))}
        </select>
      ) : (
        <Input
          aria-label={field}
          value={draft}
          placeholder={field === "release_defined_in" ? RELEASE_DEFINED_PLACEHOLDER : UNMEASURED_PLACEHOLDER}
          onChange={(e) => setDraft(e.target.value)}
        />
      )}
      {field === "base_branch" && (
        <div className="flex flex-wrap gap-1.5">
          {GITOPS_BASE_CHIPS.map((c) => (
            <TokenChip key={c} label={c} onClick={() => setDraft(c)} />
          ))}
        </div>
      )}
      {PATTERN_FIELDS.includes(field) && (
        <div className="flex flex-wrap gap-1.5">
          {GITOPS_NAMING_TOKENS.map((t) => (
            <TokenChip key={t} label={`{${t}}`} onClick={() => setDraft((d) => `${d}{${t}}`)} />
          ))}
        </div>
      )}
      {error && <p role="alert" className="text-[12px] text-st-blocked">{error}</p>}
      <div className="flex gap-2">
        <Button size="sm" onClick={onSave} disabled={!dirty || save.isPending}>
          {save.isPending ? "Saving…" : "Save"}
        </Button>
        <Button size="sm" variant="outline" disabled={save.isPending} onClick={() => setEditing(false)}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

function TokenChip({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-md border border-control px-2 py-0.5 font-mono text-small text-muted hover:text-fg-2"
    >
      {label}
    </button>
  );
}

const DEFAULT_POLICY: FleetPolicy = { local_only: false, reviewer_cross_vendor: false, allowed_harnesses: [] };

/** The new policy body: everything read back, only `caps.per_item_tokens` replaced. */
export function policyWithItemCap(current: FleetPolicy | null, cap: number | null): FleetPolicy {
  const base = current ?? DEFAULT_POLICY;
  const caps = { ...(base.caps ?? {}) };
  if (cap === null) delete caps.per_item_tokens;
  else caps.per_item_tokens = cap;
  return { ...base, caps };
}

/** `budget.cap` — stays EXTERNAL after a save; the supervisor enforces, so the NOT VERIFIED box stays. */
export function BudgetCapEditor() {
  const { activeId } = useProjectCtx();
  const { data, isLoading, isError } = useFleetPolicy(activeId);
  const save = useSaveFleetPolicy(activeId);
  const current = data?.policy ?? null;
  const saved = current?.caps?.per_item_tokens;
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState("");
  const [error, setError] = React.useState("");

  if (isLoading) return <span className={note}>Loading the fleet policy…</span>;
  if (isError || !data) return <span className={note}>The fleet policy could not be read, so it can't be edited here.</span>;

  if (!editing) {
    return (
      <div className="flex flex-col gap-1.5" data-testid="rules-budget-cap">
        <span className={cn(MONO, "text-[13px] font-semibold")}>
          {saved != null ? `${saved} tokens per item` : "— no per-item cap"}
        </span>
        <Button
          size="sm"
          variant="outline"
          className="self-start"
          onClick={() => {
            setDraft(saved != null ? String(saved) : "");
            setError("");
            setEditing(true);
          }}
        >
          Edit
        </Button>
      </div>
    );
  }

  function onSave() {
    const trimmed = draft.trim();
    const n = trimmed === "" ? null : Number(trimmed);
    if (n !== null && (!Number.isInteger(n) || n <= 0)) {
      setError("Enter a whole number of tokens, or leave it empty for no cap.");
      return;
    }
    save.mutate(policyWithItemCap(current, n), {
      onSuccess: () => setEditing(false),
      onError: (err) => setError(errorDetail(err, "Could not save the policy.")),
    });
  }

  return (
    <div className="flex flex-col gap-2" data-testid="rules-budget-editor">
      <Input
        aria-label="per_item_tokens"
        inputMode="numeric"
        value={draft}
        placeholder="No cap"
        onChange={(e) => setDraft(e.target.value)}
      />
      {error && <p role="alert" className="text-[12px] text-st-blocked">{error}</p>}
      <div className="flex gap-2">
        <Button size="sm" onClick={onSave} disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save"}
        </Button>
        <Button size="sm" variant="outline" disabled={save.isPending} onClick={() => setEditing(false)}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
