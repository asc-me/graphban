/**
 * Where a rule value comes from (GRPH-1598): platform default, org, project or org-locked, and
 * what "Reset" hands the rule back to. A value the org locked, or one this person cannot write,
 * is text with a link, never a greyed input.
 */
import { cn } from "@/lib/cn";
import type { RuleToggleSource } from "@/lib/types";

import { toggleSourceLabel } from "./levels";

const MONO = "font-mono";

export function SourceChip({ source, locked, testId }: { source: RuleToggleSource | string | null | undefined; locked?: boolean; testId: string }) {
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
      {locked ? `${toggleSourceLabel(source)} · LOCKED` : toggleSourceLabel(source)}
    </span>
  );
}

/** What a reset inherits: the org's value when the project is in an org, else the platform default. */
export function inheritsLabel(orgName: string | null): string {
  return orgName ? `the ${orgName} value` : "the platform default";
}

/** GRPH-1631: the label from the server's per-field `inherits` (the org has a row), not a guess from org membership. */
export function inheritsFor(inherits: string | undefined, orgName: string | null): string {
  return inheritsLabel(inherits === "org" ? orgName : null);
}

/** The review sheet's reset line: "→ ORG <name>" or "→ PLATFORM DEFAULT". */
export function resetTarget(inherits: string | undefined, orgName: string | null): string {
  return inherits === "org" && orgName ? `→ ORG ${orgName}` : "→ PLATFORM DEFAULT";
}

export function RuleSource({
  source,
  testId,
  locked,
  inherits,
  onReset,
  disabled,
}: {
  source: RuleToggleSource | string | null | undefined;
  testId: string;
  locked?: boolean;
  /** `inheritsLabel(...)`; null hides Reset (nothing to inherit from). */
  inherits: string | null;
  onReset?: () => void;
  disabled?: boolean;
}) {
  return (
    <span className="flex items-center gap-2">
      <SourceChip source={source} locked={locked} testId={testId} />
      {!locked && source === "project" && inherits && onReset && (
        <button
          type="button"
          disabled={disabled}
          onClick={onReset}
          data-testid={`${testId}-reset`}
          className="text-[11.5px] text-accent underline disabled:opacity-50"
        >
          Reset to {inherits}
        </button>
      )}
    </span>
  );
}

/** A value this person can't change: said in words, with the way to change it. */
export function ReadOnlyReason({ children, href, linkText }: { children: React.ReactNode; href?: string | null; linkText?: string }) {
  return (
    <span className="text-[12px] text-fg-2" data-testid="rules-readonly-reason">
      {children}
      {href && linkText && (
        <>
          {" "}
          <a href={href} className="text-accent underline">
            {linkText}
          </a>
        </>
      )}
    </span>
  );
}
