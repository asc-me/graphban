/**
 * The review sheet (GRPH-1599): every change in a stage's draft as before → after, the source
 * each value moves to, a note saying what the change does right now, ONE reason for the lot, and
 * a confirm for a change that carries risk. Saving is the caller's: this only collects the reason.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { useExclusiveAreasPreview } from "@/lib/queries";
import type { RuleParamValue } from "@/lib/types";

import { useDialogBehavior } from "./RefusalPanels";
import { EXCLUSIVE_AREAS_RULE } from "./RuleValueEditor";

const MONO = "font-mono";

export type Params = Record<string, RuleParamValue>;

export interface ParamChange {
  name: string;
  before: RuleParamValue | undefined;
  after: RuleParamValue;
  /** Set when the new value is an enum option that carries risk: what that risk is. */
  risk?: string;
}

export interface RuleChange {
  kind: "rule";
  ruleId: string;
  title: string;
  /** Present only when the rule is switched on or off. */
  on?: { before: boolean; after: boolean };
  params: ParamChange[];
  sourceBefore: string;
  sourceAfter: string;
}

export interface GitopsChange {
  kind: "gitops";
  field: string;
  title: string;
  before: string;
  after: string;
}

export type StageChange = RuleChange | GitopsChange;

export function showValue(v: RuleParamValue | undefined): string {
  if (v === undefined || v === "") return "(unset)";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "(none)";
  if (typeof v === "object") return Object.entries(v).map(([k, l]) => `${k}: ${l.join(", ")}`).join("; ") || "(none)";
  return String(v);
}

/** What a change does right now, in the server's own numbers; nothing when there is nothing to say. */
function ChangeNote({ change }: { change: RuleChange }) {
  const { activeId } = useProjectCtx();
  const paths = change.ruleId === EXCLUSIVE_AREAS_RULE ? change.params.find((p) => p.name === "paths")?.after : undefined;
  const list = Array.isArray(paths) ? paths.filter(Boolean) : [];
  const preview = useExclusiveAreasPreview(activeId, list, list.length > 0);
  const lines: string[] = [];
  if (list.length > 0 && preview.data) {
    const ids = preview.data.serialised.map((i) => i.id);
    if (ids.length === 0) lines.push("No open item would be serialised by these areas.");
    else if (ids.length === 1) lines.push(`${ids[0]} would build alone in these areas.`);
    else lines.push(`${ids.slice(0, -1).join(", ")} and ${ids[ids.length - 1]} would build one at a time.`);
  }
  for (const p of change.params) if (p.risk) lines.push(p.risk);
  if (change.on) lines.push(change.on.after ? "Turned on, this rule starts holding for this project." : "Turned off, this rule stops holding for this project.");
  if (lines.length === 0) return null;
  return (
    <ul className="flex flex-col gap-0.5" data-testid="rules-review-note">
      {lines.map((l) => (
        <li key={l} className="text-[11.5px] leading-snug text-fg-2">{l}</li>
      ))}
    </ul>
  );
}

export function riskyRules(changes: StageChange[]): RuleChange[] {
  return changes.filter((c): c is RuleChange => c.kind === "rule" && c.params.some((p) => !!p.risk));
}

export function RuleReviewSheet({
  changes,
  reason,
  onReason,
  saving,
  error,
  narrow,
  onSave,
  onBack,
}: {
  changes: StageChange[];
  reason: string;
  onReason: (v: string) => void;
  saving: boolean;
  error: string | null;
  narrow: boolean;
  onSave: () => void;
  onBack: () => void;
}) {
  const dialog = useDialogBehavior<HTMLDivElement>(onBack, { trap: true });
  const risky = riskyRules(changes);
  const [confirmed, setConfirmed] = React.useState(false);
  const needsReason = changes.some((c) => c.kind === "rule");
  const ready = !saving && (!needsReason || !!reason.trim()) && (risky.length === 0 || confirmed);
  return (
    <div className={cn("fixed inset-0 z-30 flex justify-center bg-black/50", narrow ? "items-end" : "items-center")}>
      <div
        ref={dialog.ref}
        tabIndex={-1}
        onKeyDown={dialog.onKeyDown}
        role="dialog"
        aria-label="Review rule changes"
        data-testid="rules-review-sheet"
        className={cn(
          "flex max-h-[85%] w-full flex-col gap-3 overflow-y-auto border border-line-2 bg-surface-2 p-4 shadow-2xl",
          narrow ? "rounded-t-[16px]" : "max-w-[560px] rounded-[16px]",
        )}
      >
        <span className="text-[16px] font-semibold">Review {changes.length} change{changes.length === 1 ? "" : "s"}</span>
        {changes.map((c) =>
          c.kind === "gitops" ? (
            <div key={`g:${c.field}`} className="flex flex-col gap-1 rounded-[8px] border border-[#2a333a] p-2.5" data-testid={`rules-review-gitops-${c.field}`}>
              <span className={cn(MONO, "text-[10px] tracking-[0.06em] text-muted-2")}>GITOPS SETTING · saved to the project's gitops</span>
              <span className="text-[13px] font-semibold">{c.title}</span>
              <span className={cn(MONO, "break-all text-[12px] text-fg-2")}>{showValue(c.before)} → {showValue(c.after)}</span>
            </div>
          ) : (
            <div key={c.ruleId} className="flex flex-col gap-1 rounded-[8px] border border-[#2a333a] p-2.5" data-testid={`rules-review-rule-${c.ruleId}`}>
              <span className="text-[13px] font-semibold">{c.title}</span>
              <span className={cn(MONO, "break-all text-[10.5px] text-muted")}>{c.ruleId}</span>
              {c.on && <span className={cn(MONO, "text-[12px] text-fg-2")}>{c.on.before ? "ON" : "OFF"} → {c.on.after ? "ON" : "OFF"}</span>}
              {c.params.map((p) => (
                <span key={p.name} className={cn(MONO, "break-all text-[12px] text-fg-2")}>
                  {p.name}: {showValue(p.before)} → {showValue(p.after)}
                </span>
              ))}
              <span className={cn(MONO, "text-[11px] text-muted")} data-testid="rules-review-source">
                source: {c.sourceBefore} → {c.sourceAfter}
              </span>
              <ChangeNote change={c} />
            </div>
          ),
        )}
        {needsReason && (
          <label className="flex flex-col gap-1 text-[12px] text-fg-2">
            Why? One reason covers every rule above. It is the audit record.
            <Input aria-label="Reason" autoFocus value={reason} placeholder="Say why, in a sentence" onChange={(e) => onReason(e.target.value)} />
          </label>
        )}
        {risky.length > 0 && (
          <label className="flex items-start gap-2 text-[12px] text-[#ffb454]" data-testid="rules-review-confirm-risk">
            <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
            <span>I understand the risk: {risky.map((r) => r.title).join(", ")}.</span>
          </label>
        )}
        {error && (
          <p role="alert" data-testid="rules-review-error" className="text-[12px] leading-snug text-st-blocked">
            {error}
          </p>
        )}
        <div className="flex gap-2">
          <Button size="sm" disabled={!ready} onClick={onSave} data-testid="rules-review-save">
            {saving ? "Saving…" : "Save changes"}
          </Button>
          <Button size="sm" variant="outline" disabled={saving} onClick={onBack}>
            Back to the draft
          </Button>
        </div>
      </div>
    </div>
  );
}
