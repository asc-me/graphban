/**
 * Self-host gitops Settings. Renders GitopsView only: writable greys inputs,
 * control.message is the banner, was is muted contrast — never the form value.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { useGitops, useUpdateGitops } from "@/lib/queries";
import type { GitopsField, GitopsPatch, GitopsView, GitopsWas, Project } from "@/lib/types";

export const GITOPS_BASE_CHIPS = ["stage", "test", "main", "develop"] as const;
export const GITOPS_NAMING_TOKENS = ["item_id", "tag", "slug", "version", "date"] as const;
export const UNMEASURED_PLACEHOLDER = "Unmeasured — not main";
export const UNTIL_LINKED = "This is this project's process until the box is linked.";
export const UNLINK_WARNING =
  "These are this box's pre-link values, not the org's last contract.";

/** Set by Cloud / Sync unlink — the usual path never visits Gitops while linked. */
export const GITOPS_PRELINK_KEY = "gb_gitops_prelink";

export function noteGitopsUnlinked() {
  try {
    sessionStorage.setItem(GITOPS_PRELINK_KEY, "1");
  } catch {
    /* private mode */
  }
}

const REVIEWER_OPTIONS = [
  ["", "Unmeasured"],
  ["sign_off", "Graphban sign_off"],
  ["forge", "Forge approvals"],
  ["both", "Both"],
] as const;

const VERSION_OPTIONS = [
  ["", "Unmeasured"],
  ["git_tag", "git tag"],
  ["semver", "semver"],
  ["calver", "calver"],
] as const;

type Draft = {
  base_branch: string;
  no_push_to_base: "" | "true" | "false";
  branch_name_pattern: string;
  pr_title_pattern: string;
  reviewer_bar: string;
  version_from: string;
};

function strVal(field: GitopsField): string {
  return typeof field.value === "string" ? field.value : "";
}

function triBool(field: GitopsField): "" | "true" | "false" {
  if (field.value === true) return "true";
  if (field.value === false) return "false";
  return "";
}

function fromView(view: GitopsView): Draft {
  return {
    base_branch: strVal(view.fields.base_branch),
    no_push_to_base: triBool(view.fields.no_push_to_base),
    branch_name_pattern: strVal(view.fields.branch_name_pattern),
    pr_title_pattern: strVal(view.fields.pr_title_pattern),
    reviewer_bar: strVal(view.fields.reviewer_bar),
    version_from: strVal(view.version_from),
  };
}

function rememberLinked(view: GitopsView) {
  if (view.control.state === "local") return;
  noteGitopsUnlinked();
}

function gitopsPrelinkRestored(): boolean {
  try {
    return sessionStorage.getItem(GITOPS_PRELINK_KEY) === "1";
  } catch {
    return false;
  }
}

function wasAllNull(was: GitopsWas): boolean {
  return (
    was.base_branch == null &&
    was.no_push_to_base == null &&
    was.branch_name_pattern == null &&
    was.pr_title_pattern == null &&
    was.reviewer_bar == null
  );
}

function formatWas(value: string | boolean | null | undefined): string | null {
  if (value == null) return null;
  if (typeof value === "boolean") return value ? "yes" : "no";
  return value;
}

function Label({ children }: { children: React.ReactNode }) {
  return <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">{children}</div>;
}

function Chip({
  label,
  disabled,
  onClick,
}: {
  label: string;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="rounded-md border border-line-2 px-2 py-0.5 font-mono text-[11px] text-muted hover:text-fg-2 disabled:pointer-events-none disabled:opacity-50"
    >
      {label}
    </button>
  );
}

function WasNote({ value }: { value: string | boolean | null | undefined }) {
  const shown = formatWas(value);
  if (!shown) return null;
  return <span className="ml-2 font-mono text-[10.5px] text-faint">was: {shown}</span>;
}

function Clear({ disabled, onClick }: { disabled: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-label="Clear"
      disabled={disabled}
      onClick={onClick}
      className="rounded-md border border-line-2 px-1.5 py-0.5 font-mono text-[11px] text-muted hover:text-fg-2 disabled:pointer-events-none disabled:opacity-50"
    >
      ×
    </button>
  );
}

const selectClass =
  "h-9 w-full rounded-[9px] border border-line-2 bg-surface-2 px-3 text-[13px] text-fg outline-none disabled:cursor-not-allowed";

export function GitopsPanel() {
  const { active, activeId } = useProjectCtx();
  const { data: view, isLoading } = useGitops(activeId);

  if (!activeId || !active) {
    return (
      <div className="max-w-2xl">
        <h2 className="text-[15px] font-semibold tracking-tight">Gitops</h2>
        <p className="mt-2 text-[12.5px] text-muted">
          Select a project. Gitops is per-project until the box is linked — there is no box-wide contract.
        </p>
      </div>
    );
  }

  if (isLoading || !view) {
    return <p className="text-[12.5px] text-faint">Loading gitops…</p>;
  }

  return <GitopsForm project={active} view={view} />;
}

function GitopsForm({ project, view }: { project: Project; view: GitopsView }) {
  const [draft, setDraft] = React.useState<Draft>(() => fromView(view));
  const [error, setError] = React.useState("");
  const [saved, setSaved] = React.useState(false);
  const save = useUpdateGitops(project.id);

  React.useEffect(() => {
    setDraft(fromView(view));
    rememberLinked(view);
  }, [view]);

  const writable = view.control.writable;
  const original = fromView(view);

  function patch(): GitopsPatch {
    const body: GitopsPatch = {};
    (["base_branch", "branch_name_pattern", "pr_title_pattern", "reviewer_bar", "version_from"] as const).forEach(
      (key) => {
        if (draft[key] === original[key]) return;
        body[key] = draft[key] === "" ? null : draft[key];
      },
    );
    if (draft.no_push_to_base !== original.no_push_to_base) {
      body.no_push_to_base =
        draft.no_push_to_base === "" ? null : draft.no_push_to_base === "true";
    }
    return body;
  }

  function onSave() {
    const body = patch();
    if (Object.keys(body).length === 0) return;
    save.mutate(body, {
      onSuccess: () => {
        setSaved(true);
        setError("");
        setTimeout(() => setSaved(false), 1500);
      },
      onError: (err) => setError(errorDetail(err, "Could not save gitops.")),
    });
  }

  const showUnlinkWarning = view.control.state === "local" && writable && gitopsPrelinkRestored();
  const localUntilLinked = view.control.state === "local";

  return (
    <div className="max-w-2xl space-y-5">
      <div>
        <h2 className="text-[15px] font-semibold tracking-tight">Gitops</h2>
        <p className="mt-1 text-[13px] text-fg-2">
          {project.name} · {project.tag}
        </p>
        <p className="mt-1 max-w-[60ch] text-[12.5px] leading-relaxed text-muted">
          {localUntilLinked
            ? UNTIL_LINKED
            : "The org process for the project this box is linked as."}
        </p>
      </div>

      {view.control.message ? (
        <p
          role="status"
          className="rounded-[11px] border border-st-review/30 bg-st-review/[0.06] px-3 py-2 text-[12.5px] text-st-review"
        >
          {view.control.message}
        </p>
      ) : null}

      {showUnlinkWarning ? (
        <p
          role="alert"
          className="rounded-[11px] border border-st-review/30 bg-st-review/[0.06] px-3 py-2 text-[12.5px] text-st-review"
        >
          {UNLINK_WARNING}
        </p>
      ) : null}

      {view.was && wasAllNull(view.was) ? (
        <p className="text-[12px] text-faint">This box had no local process.</p>
      ) : null}

      <div className={cn("space-y-4", !writable && "opacity-60")}>
        <Field>
          <Label>
            Base branch
            <WasNote value={view.was?.base_branch} />
          </Label>
          <div className="flex items-center gap-2">
            <Input
              aria-label="Base branch"
              value={draft.base_branch}
              disabled={!writable}
              placeholder={UNMEASURED_PLACEHOLDER}
              onChange={(e) => setDraft((d) => ({ ...d, base_branch: e.target.value }))}
            />
            <Clear disabled={!writable} onClick={() => setDraft((d) => ({ ...d, base_branch: "" }))} />
          </div>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {GITOPS_BASE_CHIPS.map((chip) => (
              <Chip
                key={chip}
                label={chip}
                disabled={!writable}
                onClick={() => setDraft((d) => ({ ...d, base_branch: chip }))}
              />
            ))}
          </div>
        </Field>

        <Field>
          <Label>
            Do not push to the base
            <WasNote value={view.was?.no_push_to_base} />
          </Label>
          <select
            aria-label="Do not push to the base"
            className={selectClass}
            disabled={!writable}
            value={draft.no_push_to_base}
            onChange={(e) =>
              setDraft((d) => ({ ...d, no_push_to_base: e.target.value as Draft["no_push_to_base"] }))
            }
          >
            <option value="">Unmeasured</option>
            <option value="true">Yes</option>
            <option value="false">No</option>
          </select>
        </Field>

        <PatternField
          label="Branch name pattern"
          ariaLabel="Branch name pattern"
          value={draft.branch_name_pattern}
          was={view.was?.branch_name_pattern}
          writable={writable}
          onChange={(branch_name_pattern) => setDraft((d) => ({ ...d, branch_name_pattern }))}
        />

        <PatternField
          label="PR title pattern"
          ariaLabel="PR title pattern"
          value={draft.pr_title_pattern}
          was={view.was?.pr_title_pattern}
          writable={writable}
          onChange={(pr_title_pattern) => setDraft((d) => ({ ...d, pr_title_pattern }))}
        />

        <Field>
          <Label>
            Reviewer bar
            <WasNote value={view.was?.reviewer_bar} />
          </Label>
          <select
            aria-label="Reviewer bar"
            className={selectClass}
            disabled={!writable}
            value={draft.reviewer_bar}
            onChange={(e) => setDraft((d) => ({ ...d, reviewer_bar: e.target.value }))}
          >
            {REVIEWER_OPTIONS.map(([value, label]) => (
              <option key={value || "unmeasured"} value={value}>
                {label}
              </option>
            ))}
          </select>
          <p className="mt-1.5 text-[11px] text-faint">
            Names which bar the process requires. Graphban sign_off is not a forge merge — both means both bars.
          </p>
        </Field>

        <Field>
          <Label>Version from</Label>
          <select
            aria-label="Version from"
            className={selectClass}
            disabled={!writable}
            value={draft.version_from}
            onChange={(e) => setDraft((d) => ({ ...d, version_from: e.target.value }))}
          >
            {VERSION_OPTIONS.map(([value, label]) => (
              <option key={value || "unmeasured"} value={value}>
                {label}
              </option>
            ))}
          </select>
          <p className="mt-1.5 text-[11px] text-faint">
            Graphban does not invent a version. git tag is <code className="font-mono">git describe --tags --abbrev=0</code> in the worktree — no tag is unmeasured, not 1.0.0.
          </p>
        </Field>
      </div>

      {error && <p className="text-[12px] text-st-blocked">{error}</p>}

      <Button size="sm" onClick={onSave} disabled={!writable || save.isPending}>
        {saved ? "Saved" : "Save gitops"}
      </Button>
    </div>
  );
}

function Field({ children }: { children: React.ReactNode }) {
  return <div>{children}</div>;
}

function PatternField({
  label,
  ariaLabel,
  value,
  was,
  writable,
  onChange,
}: {
  label: string;
  ariaLabel: string;
  value: string;
  was: string | null | undefined;
  writable: boolean;
  onChange: (next: string) => void;
}) {
  return (
    <Field>
      <Label>
        {label}
        <WasNote value={was} />
      </Label>
      <div className="flex items-center gap-2">
        <Input
          aria-label={ariaLabel}
          value={value}
          disabled={!writable}
          placeholder="Unmeasured"
          onChange={(e) => onChange(e.target.value)}
        />
        <Clear disabled={!writable} onClick={() => onChange("")} />
      </div>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        {GITOPS_NAMING_TOKENS.map((token) => (
          <Chip
            key={token}
            label={`{${token}}`}
            disabled={!writable}
            onClick={() => onChange(`${value}{${token}}`)}
          />
        ))}
      </div>
    </Field>
  );
}
