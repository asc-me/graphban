/**
 * The typed editor for one rule param (GRPH-1598). The control matches the param's type and the
 * value is saved as that type: a list is rows with add and remove, path prefixes are counted
 * against the code graph, an enum is cards that say what they change, a number is a stepper, a
 * boolean a switch and a map a table. Locked or not-writable values never reach this: they are
 * text with a link (`ReadOnlyReason`).
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { useExclusiveAreasPreview, usePathCounts } from "@/lib/queries";
import type { RuleParamValue } from "@/lib/types";

const MONO = "font-mono";
const note = "text-[11.5px] leading-snug text-muted";

export const EXCLUSIVE_AREAS_RULE = "plan.exclusive_areas";
export const GENERATED_FILES_RULE = "build.generated_files";
export const DONE_PREDICATES_RULE = "merge.done_predicates";
/** Mirrors `rule_toggles.GENERATED_FILES_DEFAULT`. */
export const GENERATED_FILES_DEFAULTS = [
  ".agents/skills/", ".claude/agents/", ".claude/skills/", ".codex/agents/", ".cursor/agents/",
  ".cursor/plugins/graphban/", ".cursor/rules/graphban.mdc", "AGENTS.md", "backend/tests/shard_weights.json",
];
/** The CI floor every project's `done` needs (`rule_toggles.CI_FLOOR_PREDICATE`). */
export const PREDICATE_FLOOR = ["suite_green"];

export interface EnumOption {
  value: string;
  label: string;
  explain: string;
  risky?: boolean;
}

/** TEST_SCOPE_MODES lives in LibraryRuleControls; it registers itself here to avoid a cycle. */
const ENUMS: Record<string, EnumOption[]> = {
  "build.undeclared_files:mode": [
    { value: "hold", label: "Hold the PR", explain: "The PR is held and a wait names the files until the touchpoints are widened." },
    {
      value: "warn",
      label: "Open the PR and warn",
      explain: "The PR is opened and the files are recorded as a wait; the merge still refuses a file no signed-off item covers.",
      risky: true,
    },
  ],
};
const GROUP_LABELS: Record<string, string> = {};
export function registerEnum(ruleId: string, param: string, options: EnumOption[], groupLabel?: string) {
  ENUMS[`${ruleId}:${param}`] = options;
  if (groupLabel) GROUP_LABELS[`${ruleId}:${param}`] = groupLabel;
}
export function enumFor(ruleId: string, param: string): EnumOption[] | undefined {
  return ENUMS[`${ruleId}:${param}`];
}

/**
 * A number param's unit and range. The server types a number param as `int` and declares no unit
 * or range for it (`rule_pack.Param` carries a type only), so these are the bounds the primitive
 * actually applies: `fleet.py` treats an effort below 0 as "any effort" and clamps
 * `min_tests_failed` to at least 1. Neither has an upper bound. A number not listed here says that
 * no unit or range is declared rather than inventing one.
 */
export interface NumberSpec {
  unit: string;
  min?: number;
  max?: number;
}
const NUMBERS: Record<string, NumberSpec> = {
  min_effort: { unit: "effort points", min: 0 },
  min_tests_failed: { unit: "failing tests", min: 1 },
};
export function numberSpecFor(name: string): NumberSpec | undefined {
  return NUMBERS[name];
}

function rangeText(spec: NumberSpec): string {
  if (spec.min !== undefined && spec.max !== undefined) return `${spec.min}–${spec.max}`;
  if (spec.min !== undefined) return `${spec.min} or more`;
  if (spec.max !== undefined) return `up to ${spec.max}`;
  return "no range";
}

export interface EditorProps {
  ruleId: string;
  name: string;
  value: RuleParamValue;
  /** The value the rule holds now; the editor shows no change marker for equal values. */
  onChange: (v: RuleParamValue) => void;
  /** Reports a draft that is not yet a value of the param's type (null clears). */
  onError: (message: string | null) => void;
}

const isMap = (v: RuleParamValue): v is Record<string, string[]> => typeof v === "object" && v !== null && !Array.isArray(v);

export function RuleValueEditor(props: EditorProps) {
  const { ruleId, name, value } = props;
  const choices = typeof value === "string" ? enumFor(ruleId, name) : undefined;
  if (choices) return <EnumCards {...props} value={value as string} options={choices} />;
  if (typeof value === "boolean") return <BoolSwitch {...props} value={value} />;
  if (typeof value === "number") return <NumberStepper {...props} value={value} />;
  if (Array.isArray(value)) {
    if (ruleId === DONE_PREDICATES_RULE && name === "predicates") return <PredicateList {...props} value={value} />;
    if (ruleId === GENERATED_FILES_RULE && name === "paths") return <GeneratedFiles {...props} value={value} />;
    if (name === "paths" || ruleId === EXCLUSIVE_AREAS_RULE) return <ListRows {...props} value={value} counted />;
    return <ListRows {...props} value={value} />;
  }
  if (isMap(value)) return <MapTable {...props} value={value} />;
  return (
    <label className={cn(MONO, "flex items-center gap-2 text-[12px] text-fg-2")}>
      <span className="w-28 flex-none break-all">{name}</span>
      <Input aria-label={name} value={String(value)} onChange={(e) => props.onChange(e.target.value)} />
    </label>
  );
}

function EnumCards({ ruleId, name, value, onChange, options }: EditorProps & { value: string; options: EnumOption[] }) {
  return (
    <div role="radiogroup" aria-label={GROUP_LABELS[`${ruleId}:${name}`] ?? name} className="flex flex-col gap-1.5" data-testid={`rules-enum-${name}`}>
      {options.map((m) => (
        <label
          key={m.value}
          className={cn(
            "flex items-start gap-2 rounded-[6px] border px-2 py-1.5 text-[12px] text-fg-2",
            value === m.value ? "border-accent" : "border-[#2a333a]",
          )}
        >
          <input type="radio" name={`${ruleId}-${name}`} value={m.value} aria-label={m.label} checked={value === m.value} onChange={() => onChange(m.value)} />
          <span>
            <span className="text-fg">{m.label}</span>
            {m.risky && <span className={cn(MONO, "ml-2 text-[10px] text-[#ffb454]")}>RISKY</span>}
            <span className="block text-muted-2">{m.explain}</span>
          </span>
        </label>
      ))}
    </div>
  );
}

function BoolSwitch({ name, value, onChange }: EditorProps & { value: boolean }) {
  return (
    <label className={cn(MONO, "flex items-center gap-2 text-[12px] text-fg-2")}>
      <button
        type="button"
        role="switch"
        aria-checked={value}
        aria-label={name}
        onClick={() => onChange(!value)}
        className={cn(
          "flex h-[18px] w-[32px] items-center rounded-full border px-[2px]",
          value ? "justify-end border-accent bg-accent/20" : "justify-start border-control bg-surface-2",
        )}
      >
        <span className={cn("h-3 w-3 rounded-full", value ? "bg-accent" : "bg-muted")} />
      </button>
      {name}
    </label>
  );
}

function NumberStepper({ name, value, onChange, onError }: EditorProps & { value: number }) {
  const whole = Number.isInteger(value);
  const spec = numberSpecFor(name);
  const [text, setText] = React.useState(String(value));
  const [err, setErr] = React.useState<string | null>(null);
  React.useEffect(() => {
    setText(String(value));
    setErr(null);
  }, [value]);
  function commit(t: string) {
    setText(t);
    const n = Number(t.trim());
    let m: string | null = null;
    if (t.trim() === "" || !Number.isFinite(n) || (whole && !Number.isInteger(n))) m = `${name} must be ${whole ? "a whole number" : "a number"}.`;
    else if (spec && ((spec.min !== undefined && n < spec.min) || (spec.max !== undefined && n > spec.max)))
      m = `${name} must be ${rangeText(spec)} ${spec.unit}.`;
    if (m) {
      setErr(m);
      onError(m);
      return;
    }
    setErr(null);
    onError(null);
    onChange(n);
  }
  const current = Number(text.trim());
  const base = Number.isFinite(current) ? current : value;
  const step = (d: number) => commit(String(base + d));
  const atMin = spec?.min !== undefined && base <= spec.min;
  const atMax = spec?.max !== undefined && base >= spec.max;
  return (
    <div className="flex flex-col gap-1">
      <div className={cn(MONO, "flex items-center gap-2 text-[12px] text-fg-2")}>
        <span className="w-28 flex-none break-all">{name}</span>
        <Button type="button" size="sm" variant="outline" aria-label={`Decrease ${name}`} disabled={atMin} onClick={() => step(-1)}>
          −
        </Button>
        <Input
          aria-label={name}
          inputMode="numeric"
          value={text}
          min={spec?.min}
          max={spec?.max}
          onChange={(e) => commit(e.target.value)}
        />
        <Button type="button" size="sm" variant="outline" aria-label={`Increase ${name}`} disabled={atMax} onClick={() => step(1)}>
          +
        </Button>
        {spec && <span className="flex-none text-muted-2">{spec.unit}</span>}
      </div>
      <span className={note} data-testid={`rules-number-range-${name}`}>
        {spec ? `Range: ${rangeText(spec)} ${spec.unit}.` : "The rule pack declares no unit or range for this number."}
      </span>
      {err && (
        <p role="alert" className="text-[12px] text-st-blocked">
          {err}
        </p>
      )}
    </div>
  );
}

function ListRows({ ruleId, name, value, onChange, counted }: EditorProps & { value: string[]; counted?: boolean }) {
  const { activeId } = useProjectCtx();
  const rows = value;
  const set = (next: string[]) => onChange(next);
  const entries = rows.map((r) => r.trim()).filter(Boolean);
  return (
    <div className="flex flex-col gap-1.5" role="group" aria-label={name} data-testid={`rules-list-${name}`}>
      <span className={cn(MONO, "text-[10px] text-muted-2")}>{name}</span>
      {rows.map((r, i) => (
        <div key={i} className="flex items-center gap-2">
          <Input aria-label={`${name} ${i + 1}`} value={r} onChange={(e) => set(rows.map((x, j) => (j === i ? e.target.value : x)))} />
          {counted && <PathCountBadge projectId={activeId} path={r} />}
          <Button type="button" size="sm" variant="outline" aria-label={`Remove ${name} ${i + 1}`} onClick={() => set(rows.filter((_, j) => j !== i))}>
            Remove
          </Button>
        </div>
      ))}
      <Button type="button" size="sm" variant="outline" className="self-start" onClick={() => set([...rows, ""])}>
        Add {name === "paths" ? "path" : "entry"}
      </Button>
      {counted && <CountsNote projectId={activeId} entries={entries} />}
      {ruleId === EXCLUSIVE_AREAS_RULE && <SerialisationNote projectId={activeId} entries={entries} />}
    </div>
  );
}

/** One prefix's count. A graph that can't count says so; it is never drawn as 0. */
function PathCountBadge({ projectId, path }: { projectId: string; path: string }) {
  const trimmed = path.trim();
  const q = usePathCounts(projectId, trimmed ? [trimmed] : [], !!trimmed);
  if (!trimmed) return null;
  const row = q.data?.paths[0];
  let text: string;
  let warn = false;
  if (q.isLoading) text = "counting…";
  else if (q.isError || !row || row.files === null) text = "can't count";
  else if (row.files === 0) {
    text = "matches no files";
    warn = true;
  } else text = `${row.files} file${row.files === 1 ? "" : "s"}`;
  return (
    <span data-testid="rules-path-count" className={cn(MONO, "flex-none text-[11px]", warn ? "text-[#ffb454]" : "text-muted-2")}>
      {warn && "⚠ "}
      {row?.normalised && row.normalised !== trimmed ? `${row.normalised} · ` : ""}
      {text}
    </span>
  );
}

function CountsNote({ projectId, entries }: { projectId: string; entries: string[] }) {
  const q = usePathCounts(projectId, entries, entries.length > 0);
  if (entries.length === 0 || q.isLoading) return null;
  if (q.isError || (q.data && !q.data.indexed)) {
    return (
      <p className={note} data-testid="rules-counts-unavailable">
        The code graph can't count files for this project, so no counts are shown.
      </p>
    );
  }
  return null;
}

function SerialisationNote({ projectId, entries }: { projectId: string; entries: string[] }) {
  const q = useExclusiveAreasPreview(projectId, entries, entries.length > 0);
  if (!q.data) return null;
  const n = q.data.serialised.length;
  return (
    <p className={note} data-testid="rules-serialisation-preview">
      {n === 0 ? "No open item would be serialised by these areas." : `${n} open item${n === 1 ? "" : "s"} would be serialised: ${q.data.serialised.map((i) => i.id).join(", ")}.`}
    </p>
  );
}

/** Generated files: whether your entries add to the built-in list or replace it is a choice. */
function GeneratedFiles(props: EditorProps & { value: string[] }) {
  const { value, onChange } = props;
  const defaults = GENERATED_FILES_DEFAULTS;
  const [mode, setMode] = React.useState<"add" | "replace">(() => (defaults.every((d) => value.includes(d)) ? "add" : "replace"));
  const extras = mode === "add" ? value.filter((v) => !defaults.includes(v)) : value;
  const emit = (m: "add" | "replace", list: string[]) => onChange(m === "add" ? [...defaults, ...list] : list);
  return (
    <div className="flex flex-col gap-1.5" data-testid="rules-generated-files">
      <div role="radiogroup" aria-label="Generated files" className="flex flex-col gap-1">
        {(
          [
            ["add", "Add to defaults", "Keep the built-in generated files and add yours."],
            ["replace", "Replace defaults", "Only the files listed here are ignored."],
          ] as const
        ).map(([v, label, explain]) => (
          <label key={v} className="flex items-start gap-2 text-[12px] text-fg-2">
            <input
              type="radio"
              name="generated-files-mode"
              aria-label={label}
              checked={mode === v}
              onChange={() => {
                setMode(v);
                emit(v, extras);
              }}
            />
            <span>
              <span className="text-fg">{label}</span>
              <span className="block text-muted-2">{explain}</span>
            </span>
          </label>
        ))}
      </div>
      <ListRows {...props} value={extras} onChange={(l) => emit(mode, l as string[])} counted />
    </div>
  );
}

/** Done predicates: the floor is fixed rows; additions are free text, `scans_clean` suggested. */
function PredicateList(props: EditorProps & { value: string[] }) {
  const { value, onChange } = props;
  const extras = value.filter((p) => !PREDICATE_FLOOR.includes(p));
  const emit = (list: string[]) => onChange([...PREDICATE_FLOOR, ...list]);
  return (
    <div className="flex flex-col gap-1.5" data-testid="rules-predicates">
      <span className={cn(MONO, "text-[10px] text-muted-2")}>predicates</span>
      {PREDICATE_FLOOR.map((p) => (
        <span key={p} className={cn(MONO, "text-[12px] text-fg-2")} data-testid="rules-predicate-floor">
          {p} · required
        </span>
      ))}
      <span className={note}>The deployment's own required predicates also apply and cannot be dropped.</span>
      <ListRows {...props} name="additions" value={extras} onChange={(l) => emit(l as string[])} />
      {!extras.includes("scans_clean") && (
        <Button type="button" size="sm" variant="outline" className="self-start" onClick={() => emit([...extras, "scans_clean"])}>
          Suggested: add scans_clean
        </Button>
      )}
    </div>
  );
}

function MapTable({ name, value, onChange }: EditorProps & { value: Record<string, string[]> }) {
  const rows = Object.entries(value);
  const write = (next: [string, string[]][]) => onChange(Object.fromEntries(next));
  return (
    <div className="flex flex-col gap-1.5" role="group" aria-label={name} data-testid={`rules-map-${name}`}>
      <span className={cn(MONO, "text-[10px] text-muted-2")}>{name}</span>
      {rows.map(([k, list], i) => (
        <div key={i} className="flex items-center gap-2">
          <Input aria-label={`${name} key ${i + 1}`} value={k} onChange={(e) => write(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} />
          <Input
            aria-label={`${name} values ${i + 1}`}
            value={list.join(", ")}
            onChange={(e) => write(rows.map((r, j) => (j === i ? [r[0], e.target.value.split(",").map((s) => s.trim()).filter(Boolean)] : r)))}
          />
          <Button type="button" size="sm" variant="outline" aria-label={`Remove ${name} row ${i + 1}`} onClick={() => write(rows.filter((_, j) => j !== i))}>
            Remove
          </Button>
        </div>
      ))}
      <Button type="button" size="sm" variant="outline" className="self-start" onClick={() => write([...rows, ["", []]])}>
        Add row
      </Button>
    </div>
  );
}
