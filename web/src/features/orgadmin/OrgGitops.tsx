import { X } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorDetail } from "@/lib/errors";
import {
  useGitops,
  useOrgs,
  useOrgGitops,
  useUpdateGitops,
  useUpdateOrgGitops,
} from "@/lib/queries";
import type { GitopsField, GitopsPatch, GitopsProjectRef, GitopsView } from "@/lib/types";

/**
 * Hosted Admin → Gitops (GRPH-P31 PR 3).
 *
 * Roster is `GitopsView.projects` from the org GET, not the readable-only project list
 * — that would hide a project the admin does not sit on. Overlay inputs bind
 * source === "project" ? value : empty so an inherited `stage` cannot copy-down on save.
 */

const BASE_CHIPS = ["stage", "test", "main", "develop"] as const;
const TOKEN_CHIPS = ["{item_id}", "{tag}", "{slug}", "{version}", "{date}"] as const;

const SELECT_CLASS =
  "h-9 rounded-[9px] border border-line-2 bg-surface-2 px-2 text-[13px] text-fg outline-none focus:border-line-hover";
const CHIP_CLASS =
  "rounded-[5px] border border-line px-1.5 py-px font-mono text-[10px] text-muted hover:border-line-hover hover:text-fg disabled:opacity-50";

type PatchKey = keyof GitopsPatch;
type Edits = GitopsPatch;

function fieldStr(field: GitopsField<string | boolean | null>): string {
  if (field.value === null || field.value === undefined) return "";
  if (typeof field.value === "boolean") return field.value ? "true" : "false";
  return String(field.value);
}

function overlayBound(field: GitopsField<string | boolean | null>): string {
  return field.source === "project" ? fieldStr(field) : "";
}

function inheritHint(field: GitopsField<string | boolean | null>): string | undefined {
  if (field.source !== "org" || field.value === null || field.value === undefined) return undefined;
  if (typeof field.value === "boolean") return `inherits ${field.value ? "Yes" : "No"}`;
  return `inherits ${field.value}`;
}

function shown(edits: Edits, key: PatchKey, bound: string): string {
  if (!Object.prototype.hasOwnProperty.call(edits, key)) return bound;
  const v = edits[key];
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "true" : "false";
  return v;
}

function setEdit(edits: Edits, key: PatchKey, bound: string, next: string | boolean | null): Edits {
  if (next === null) return { ...edits, [key]: null };
  const asStr = typeof next === "boolean" ? (next ? "true" : "false") : next;
  if (asStr === bound || (typeof next === "string" && next === "")) {
    const { [key]: _drop, ...rest } = edits;
    return rest;
  }
  return { ...edits, [key]: next };
}

export function OrgGitops() {
  const { data: orgs = [] } = useOrgs();
  const org = orgs[0] ?? null;
  const { data: house, isLoading, isError, error } = useOrgGitops(org?.id);

  if (!org) {
    return (
      <div className="max-w-[1180px] px-6 py-8 font-mono text-[11px] text-faint-2">loading…</div>
    );
  }
  if (isLoading) {
    return (
      <div className="max-w-[1180px] px-6 py-8 font-mono text-[11px] text-faint-2">loading…</div>
    );
  }
  if (isError || !house) {
    return (
      <div className="max-w-[1180px] px-6 py-8 text-[13px] text-st-blocked">
        {errorDetail(error, "could not load house process")}
      </div>
    );
  }

  return (
    <div className="max-w-[1180px] px-6 pb-16 pt-5">
      <p className="mb-4 max-w-[80ch] text-[12.5px] leading-relaxed text-muted">
        House process for this organization. Unset fields are{" "}
        <span className="text-fg-2">unmeasured</span> — not main and not “no requirements”.
        A project overlay is empty until someone sets it; empty inherits.
      </p>
      <HouseForm orgId={org.id} view={house} />
      <OverlayRoster projects={house.projects} />
    </div>
  );
}

function HouseForm({ orgId, view }: { orgId: string; view: GitopsView }) {
  const save = useUpdateOrgGitops(orgId);
  const [edits, setEdits] = React.useState<Edits>({});
  const writable = view.control.writable;

  const boundOf = (key: PatchKey): string => {
    if (key === "version_from") return fieldStr(view.version_from);
    return fieldStr(view.fields[key as keyof GitopsView["fields"]]);
  };

  const onSave = () => {
    save.mutate(edits, { onSuccess: () => setEdits({}) });
  };

  return (
    <section className="rounded-[13px] border border-line bg-surface-2 p-4">
      <h2 className="text-[15px] font-semibold">House process</h2>
      <p className="mt-1 max-w-[70ch] text-[12.5px] leading-relaxed text-muted">
        Applies to every project unless that project sets an overlay.
      </p>
      <div className="mt-4 flex flex-col gap-3">
        <ProcessFields
          prefix="House"
          edits={edits}
          boundOf={boundOf}
          hintOf={() => undefined}
          placeholderOf={(key) =>
            key === "base_branch" ? "Unmeasured — not main" : undefined
          }
          writable={writable}
          overlay={false}
          onEdit={(key, bound, next) => setEdits((p) => setEdit(p, key, bound, next))}
        />
      </div>
      <div className="mt-4 flex items-center gap-3">
        <Button type="button" size="sm" disabled={!writable || save.isPending} onClick={onSave}>
          Save house process
        </Button>
        {save.isError && (
          <span className="text-[12px] text-st-blocked">
            {errorDetail(save.error, "save failed")}
          </span>
        )}
      </div>
    </section>
  );
}

function OverlayRoster({ projects }: { projects: GitopsProjectRef[] }) {
  return (
    <section className="mt-6">
      <h2 className="text-[15px] font-semibold">Project overlay</h2>
      <p className="mt-1 max-w-[70ch] text-[12.5px] leading-relaxed text-muted">
        Empty means inherit. × clears the overlay. Saving with no edits sends nothing.
      </p>
      {projects.length === 0 ? (
        <div className="mt-3 rounded-[13px] border border-line bg-surface-2 px-5 py-6 font-mono text-[11px] text-faint-2">
          no projects yet
        </div>
      ) : (
        <div className="mt-3 flex flex-col gap-3">
          {projects.map((p) => (
            <OverlayRow key={p.id} project={p} />
          ))}
        </div>
      )}
    </section>
  );
}

function OverlayRow({ project }: { project: GitopsProjectRef }) {
  const { data, isError, isLoading } = useGitops(project.id);
  const save = useUpdateGitops(project.id);
  const [edits, setEdits] = React.useState<Edits>({});
  const prefix = project.tag;

  const onSave = () => {
    // No-edit must PATCH {} — a form filled with resolved org values would copy-down.
    save.mutate(edits, { onSuccess: () => setEdits({}) });
  };

  return (
    <div className="rounded-[13px] border border-line bg-surface-2 p-4">
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-[11px] text-accent">{project.tag}</span>
        <span className="text-[13px] font-medium">{project.name}</span>
      </div>
      {isLoading ? (
        <div className="mt-3 font-mono text-[11px] text-faint-2">loading overlay…</div>
      ) : isError || !data ? (
        <div className="mt-3">
          <p className="text-[12.5px] text-st-blocked">could not load overlay</p>
          <p className="mt-1 text-[11px] text-faint">unmeasured — this row, not the house form</p>
        </div>
      ) : (
        <>
          <div className="mt-3 flex flex-col gap-3">
            <ProcessFields
              prefix={`${prefix} overlay`}
              edits={edits}
              boundOf={(key) => {
                if (key === "version_from") return overlayBound(data.version_from);
                return overlayBound(data.fields[key as keyof GitopsView["fields"]]);
              }}
              hintOf={(key) => {
                if (key === "version_from") return inheritHint(data.version_from);
                return inheritHint(data.fields[key as keyof GitopsView["fields"]]);
              }}
              placeholderOf={(key) => {
                if (key !== "base_branch") return undefined;
                const f = data.fields.base_branch;
                if (f.source === "org" && typeof f.value === "string" && f.value) {
                  return `inherits ${f.value}`;
                }
                return "inherit";
              }}
              writable={data.control.writable}
              overlay
              onEdit={(key, bound, next) => setEdits((p) => setEdit(p, key, bound, next))}
            />
          </div>
          <div className="mt-4 flex items-center gap-3">
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={!data.control.writable || save.isPending}
              onClick={onSave}
            >
              Save {prefix} overlay
            </Button>
            {save.isError && (
              <span className="text-[12px] text-st-blocked">
                {errorDetail(save.error, "save failed")}
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function ProcessFields({
  prefix,
  edits,
  boundOf,
  hintOf,
  placeholderOf,
  writable,
  overlay,
  onEdit,
}: {
  prefix: string;
  edits: Edits;
  boundOf: (key: PatchKey) => string;
  hintOf: (key: PatchKey) => string | undefined;
  placeholderOf: (key: PatchKey) => string | undefined;
  writable: boolean;
  overlay: boolean;
  onEdit: (key: PatchKey, bound: string, next: string | boolean | null) => void;
}) {
  const emptyLabel = overlay ? "Inherit" : "Unmeasured";
  return (
    <>
      <TextField
        label="Base branch"
        ariaLabel={`${prefix} base branch`}
        value={shown(edits, "base_branch", boundOf("base_branch"))}
        placeholder={placeholderOf("base_branch") ?? (overlay ? "inherit" : "Unmeasured — not main")}
        hint={hintOf("base_branch")}
        writable={writable}
        chips={BASE_CHIPS}
        onChips={(chip) => onEdit("base_branch", boundOf("base_branch"), chip)}
        onChange={(v) => onEdit("base_branch", boundOf("base_branch"), v)}
        onClear={() => onEdit("base_branch", boundOf("base_branch"), null)}
      />
      <SelectField
        label="No push to base"
        ariaLabel={`${prefix} no push to base`}
        value={shown(edits, "no_push_to_base", boundOf("no_push_to_base"))}
        hint={hintOf("no_push_to_base")}
        writable={writable}
        emptyLabel={emptyLabel}
        options={[
          { value: "true", label: "Yes" },
          { value: "false", label: "No" },
        ]}
        onChange={(v) =>
          onEdit(
            "no_push_to_base",
            boundOf("no_push_to_base"),
            v === "" ? (boundOf("no_push_to_base") ? null : "") : v === "true",
          )
        }
        onClear={() => onEdit("no_push_to_base", boundOf("no_push_to_base"), null)}
      />
      <TextField
        label="Branch name pattern"
        ariaLabel={`${prefix} branch name pattern`}
        value={shown(edits, "branch_name_pattern", boundOf("branch_name_pattern"))}
        hint={hintOf("branch_name_pattern")}
        writable={writable}
        chips={TOKEN_CHIPS}
        onChips={(chip) => {
          const cur = shown(edits, "branch_name_pattern", boundOf("branch_name_pattern"));
          onEdit("branch_name_pattern", boundOf("branch_name_pattern"), cur + chip);
        }}
        onChange={(v) => onEdit("branch_name_pattern", boundOf("branch_name_pattern"), v)}
        onClear={() => onEdit("branch_name_pattern", boundOf("branch_name_pattern"), null)}
      />
      <TextField
        label="PR title pattern"
        ariaLabel={`${prefix} PR title pattern`}
        value={shown(edits, "pr_title_pattern", boundOf("pr_title_pattern"))}
        hint={hintOf("pr_title_pattern")}
        writable={writable}
        chips={TOKEN_CHIPS}
        onChips={(chip) => {
          const cur = shown(edits, "pr_title_pattern", boundOf("pr_title_pattern"));
          onEdit("pr_title_pattern", boundOf("pr_title_pattern"), cur + chip);
        }}
        onChange={(v) => onEdit("pr_title_pattern", boundOf("pr_title_pattern"), v)}
        onClear={() => onEdit("pr_title_pattern", boundOf("pr_title_pattern"), null)}
      />
      <SelectField
        label="Reviewer bar"
        ariaLabel={`${prefix} reviewer bar`}
        value={shown(edits, "reviewer_bar", boundOf("reviewer_bar"))}
        hint={hintOf("reviewer_bar")}
        writable={writable}
        emptyLabel={emptyLabel}
        options={[
          { value: "sign_off", label: "Graphban sign_off" },
          { value: "forge", label: "Forge approvals" },
          { value: "both", label: "Both" },
        ]}
        onChange={(v) =>
          onEdit(
            "reviewer_bar",
            boundOf("reviewer_bar"),
            v === "" ? (boundOf("reviewer_bar") ? null : "") : v,
          )
        }
        onClear={() => onEdit("reviewer_bar", boundOf("reviewer_bar"), null)}
      />
      <SelectField
        label="Version from"
        ariaLabel={`${prefix} version from`}
        value={shown(edits, "version_from", boundOf("version_from"))}
        hint={hintOf("version_from")}
        writable={writable}
        emptyLabel={emptyLabel}
        options={[
          { value: "git_tag", label: "git tag" },
          { value: "semver", label: "semver" },
          { value: "calver", label: "calver" },
        ]}
        onChange={(v) =>
          onEdit(
            "version_from",
            boundOf("version_from"),
            v === "" ? (boundOf("version_from") ? null : "") : v,
          )
        }
        onClear={() => onEdit("version_from", boundOf("version_from"), null)}
      />
    </>
  );
}

function TextField({
  label,
  ariaLabel,
  value,
  placeholder,
  hint,
  writable,
  chips,
  onChange,
  onChips,
  onClear,
}: {
  label: string;
  ariaLabel: string;
  value: string;
  placeholder?: string;
  hint?: string;
  writable: boolean;
  chips?: readonly string[];
  onChange: (v: string) => void;
  onChips: (chip: string) => void;
  onClear: () => void;
}) {
  return (
    <FieldRow label={label} hint={hint}>
      <div className="flex items-center gap-1.5">
        <Input
          aria-label={ariaLabel}
          value={value}
          placeholder={placeholder}
          disabled={!writable}
          onChange={(e) => onChange(e.target.value)}
        />
        {value !== "" && (
          <button
            type="button"
            aria-label={`Clear ${ariaLabel}`}
            disabled={!writable}
            onClick={onClear}
            className="flex h-9 w-9 items-center justify-center rounded-[9px] border border-line text-faint hover:text-fg disabled:opacity-50"
          >
            <X size={12} />
          </button>
        )}
      </div>
      {chips && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {chips.map((c) => (
            <button
              key={c}
              type="button"
              disabled={!writable}
              className={CHIP_CLASS}
              onClick={() => onChips(c)}
            >
              {c}
            </button>
          ))}
        </div>
      )}
    </FieldRow>
  );
}

function SelectField({
  label,
  ariaLabel,
  value,
  hint,
  writable,
  emptyLabel,
  options,
  onChange,
  onClear,
}: {
  label: string;
  ariaLabel: string;
  value: string;
  hint?: string;
  writable: boolean;
  emptyLabel: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
  onClear: () => void;
}) {
  return (
    <FieldRow label={label} hint={hint}>
      <div className="flex items-center gap-1.5">
        <select
          aria-label={ariaLabel}
          value={value}
          disabled={!writable}
          onChange={(e) => onChange(e.target.value)}
          className={SELECT_CLASS}
        >
          <option value="">{emptyLabel}</option>
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        {value !== "" && (
          <button
            type="button"
            aria-label={`Clear ${ariaLabel}`}
            disabled={!writable}
            onClick={onClear}
            className="flex h-9 w-9 items-center justify-center rounded-[9px] border border-line text-faint hover:text-fg disabled:opacity-50"
          >
            <X size={12} />
          </button>
        )}
      </div>
    </FieldRow>
  );
}

function FieldRow({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5 sm:flex-row sm:items-start sm:gap-3">
      <span className="w-[170px] shrink-0 pt-2 font-mono text-[9.5px] uppercase tracking-[0.06em] text-faint">
        {label}
      </span>
      <div className="min-w-0 flex-1">
        {children}
        {hint && <div className="mt-1 text-[11px] text-faint">{hint}</div>}
      </div>
    </div>
  );
}
