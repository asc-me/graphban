/**
 * The project theme controls (GRPH-1750, GRPH-1802).
 *
 * One picker for the new-project wizard and for Settings → Project: Solid/Ombre,
 * the twelve swatches, the second-colour row, the pattern row, and the live tile.
 * Both paint through `projectFill`. The keyboard model is the WAI-ARIA radio group.
 */
import * as React from "react";

import { ProjectTile } from "@/components/ProjectTile";
import { PROJECT_ACCENTS, PROJECT_ACCENT_SWATCHES } from "@/features/onboarding/projectAccents";
import { cn } from "@/lib/cn";
import { PROJECT_PATTERNS, projectFill } from "@/lib/projectFill";
import type { Project, ProjectPattern } from "@/lib/types";

/** The WAI-ARIA radio group keyboard model (GRPH-1613, GRPH-1618). ArrowRight/ArrowDown select
 *  the next option and ArrowLeft/ArrowUp the previous, wrapping at the ends, on either axis as
 *  the pattern says; Home and End go to the first and last. Disabled options are skipped, and
 *  focus follows the checked option. A locked group ignores the keys. */
export function radioArrows<T>(
  e: React.KeyboardEvent<HTMLElement>,
  o: {
    values: readonly T[];
    current: T;
    set: (v: T) => void;
    locked?: boolean;
    disabled?: (v: T) => boolean;
  },
) {
  if (o.locked) return;
  const n = o.values.length;
  const usable = (i: number) => !o.disabled?.(o.values[i]);
  const from = o.values.indexOf(o.current);
  let i = -1;
  if (e.key === "Home" || e.key === "End") {
    const order = [...Array(n).keys()];
    if (e.key === "End") order.reverse();
    i = order.find(usable) ?? -1;
  } else {
    const step =
      e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!step) return;
    for (let k = 1; k <= n; k++) {
      const j = (((from + step * k) % n) + n) % n;
      if (usable(j)) {
        i = j;
        break;
      }
    }
  }
  e.preventDefault();
  if (i < 0 || i === from) return;
  o.set(o.values[i]);
  e.currentTarget.closest('[role="radiogroup"]')?.querySelectorAll<HTMLElement>('[role="radio"]')[i]?.focus();
}

/** Selection ring. Box-shadow, not outline, so the global :focus-visible ring stays visible. */
const SWATCH_RING = "0 0 0 2px var(--color-surface-3), 0 0 0 4px var(--color-fg)";
const COLOURS = PROJECT_ACCENTS as readonly string[];
const MODES = [false, true] as const;
const LABEL = "mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint";

/** Four steps along the palette. An ombre moves its second colour here when the two would match. */
export function shiftAccent(hex: string): string {
  const i = COLOURS.indexOf(hex);
  return COLOURS[(Math.max(0, i) + 4) % COLOURS.length]!;
}

function accentName(hex: string): string {
  return PROJECT_ACCENT_SWATCHES.find((swatch) => swatch.hex === hex)?.name ?? hex;
}

/** "Lime → Indigo ombre · paisley", or "Lime · no pattern" when the fill is solid and plain. */
export function accentSummary(ombre: boolean, accent: string, accent2: string, pattern: ProjectPattern): string {
  const named = PROJECT_PATTERNS.find((p) => p.id === pattern)?.name ?? "None";
  const tail = pattern === "none" ? "no pattern" : named.toLowerCase();
  if (ombre && accent2 !== accent) return `${accentName(accent)} → ${accentName(accent2)} ombre · ${tail}`;
  return `${accentName(accent)} · ${tail}`;
}

/** What the picker edits. `accent2` is kept while solid so returning to ombre restores it. */
export interface ProjectThemeValue {
  accent: string;
  accent2: string;
  ombre: boolean;
  pattern: ProjectPattern;
}

/** Settings reads a stored project. A null second colour is solid; a missing pattern is none. */
export function themeFromProject(project: {
  accent?: string | null;
  accent2?: string | null;
  pattern?: ProjectPattern | null;
}): ProjectThemeValue {
  const accent = project.accent || PROJECT_ACCENTS[0];
  return {
    accent,
    accent2: project.accent2 || shiftAccent(accent),
    ombre: !!project.accent2,
    pattern: project.pattern ?? "none",
  };
}

/**
 * Theme fields that differ from the stored project. Unchanged colours and patterns
 * are left out. Solid sends `accent2: null`. "None" sends `pattern: "none"`.
 */
export function themePatch(
  stored: { accent?: string | null; accent2?: string | null; pattern?: ProjectPattern | null },
  theme: ProjectThemeValue,
): Partial<Pick<Project, "accent" | "accent2" | "pattern">> {
  const accent2 = theme.ombre ? theme.accent2 : null;
  const patch: Partial<Pick<Project, "accent" | "accent2" | "pattern">> = {};
  if (theme.accent !== (stored.accent || "")) patch.accent = theme.accent;
  if ((stored.accent2 ?? null) !== accent2) patch.accent2 = accent2;
  if ((stored.pattern ?? "none") !== theme.pattern) patch.pattern = theme.pattern;
  return patch;
}

function FillSwatch({
  name,
  fill,
  on,
  disabled,
  dimmed,
  onClick,
  onKeyDown,
  pattern,
}: {
  name: string;
  fill: string;
  on: boolean;
  disabled?: boolean;
  dimmed?: boolean;
  onClick: () => void;
  onKeyDown: (e: React.KeyboardEvent<HTMLButtonElement>) => void;
  pattern?: boolean;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={on}
      aria-label={name}
      title={name}
      tabIndex={on ? 0 : -1}
      disabled={disabled}
      onClick={onClick}
      onKeyDown={onKeyDown}
      className={cn(
        "rounded-[8px] p-0 transition-transform hover:scale-110 disabled:hover:scale-100",
        pattern ? "h-7 w-7" : "h-[26px] w-[26px]",
      )}
      style={
        {
          "--tile-fill": fill,
          background: "var(--tile-fill)",
          boxShadow: on ? SWATCH_RING : undefined,
          outlineOffset: "5px",
          opacity: dimmed ? 0.25 : undefined,
        } as React.CSSProperties
      }
    />
  );
}

export function ProjectThemePicker({
  value,
  onChange,
  disabled,
  tag,
  still,
}: {
  value: ProjectThemeValue;
  onChange: (next: ProjectThemeValue) => void;
  disabled?: boolean;
  tag: string;
  still?: boolean;
}) {
  const { accent, accent2, ombre, pattern } = value;
  const sentAccent2 = ombre ? accent2 : null;

  function chooseAccent(hex: string) {
    if (disabled) return;
    const next = { ...value, accent: hex };
    // An ombre never ends on its own start. Moving FROM onto TO pushes TO four steps on.
    if (ombre && hex === accent2) next.accent2 = shiftAccent(hex);
    onChange(next);
  }

  function chooseMode(nextMode: boolean) {
    if (disabled) return;
    const next = { ...value, ombre: nextMode };
    if (nextMode && accent2 === accent) next.accent2 = shiftAccent(accent);
    onChange(next);
  }

  function chooseSecond(hex: string) {
    if (disabled || hex === accent) return;
    onChange({ ...value, accent2: hex });
  }

  function choosePattern(id: ProjectPattern) {
    if (disabled) return;
    onChange({ ...value, pattern: id });
  }

  return (
    <div className="flex flex-col gap-[9px]">
      <div className="flex items-center gap-2.5">
        <span className={cn(LABEL, "mb-0")}>Accent</span>
        <span className="flex-1" />
        <div
          role="radiogroup"
          aria-label="Accent style"
          className="inline-flex rounded-[8px] border border-line-2 bg-surface p-0.5"
        >
          {MODES.map((mode) => {
            const on = ombre === mode;
            return (
              <button
                key={mode ? "ombre" : "solid"}
                type="button"
                role="radio"
                aria-checked={on}
                tabIndex={on ? 0 : -1}
                disabled={disabled}
                onClick={() => chooseMode(mode)}
                onKeyDown={(e) =>
                  radioArrows(e, { values: MODES, current: ombre, set: chooseMode, locked: disabled })
                }
                className={cn(
                  "h-6 rounded-[6px] px-2.5 font-mono text-[10.5px] tracking-wide",
                  on ? "bg-accent/[.14] text-fg" : "text-muted",
                )}
                style={{ boxShadow: on ? SWATCH_RING : undefined, outlineOffset: "5px" }}
              >
                {mode ? "OMBRE" : "SOLID"}
              </button>
            );
          })}
        </div>
      </div>
      <div className="flex items-start gap-[18px]">
        <div className="flex min-w-0 flex-1 flex-col gap-3">
          <div className="flex items-center gap-2.5">
            {ombre && (
              <span className="w-[30px] shrink-0 font-mono text-meta tracking-wide text-faint">FROM</span>
            )}
            <div
              role="radiogroup"
              aria-label={ombre ? "Ombre, first colour" : "Accent colour"}
              className="flex flex-wrap gap-2"
            >
              {PROJECT_ACCENT_SWATCHES.map((swatch) => (
                <FillSwatch
                  key={swatch.hex}
                  name={swatch.name}
                  fill={projectFill({ accent: swatch.hex, accent2: sentAccent2, pattern })}
                  on={accent === swatch.hex}
                  disabled={disabled}
                  onClick={() => chooseAccent(swatch.hex)}
                  onKeyDown={(e) =>
                    radioArrows(e, {
                      values: COLOURS,
                      current: accent,
                      set: chooseAccent,
                      locked: disabled,
                    })
                  }
                />
              ))}
            </div>
          </div>
          {ombre && (
            <div
              className="pw-anim flex items-center gap-2.5"
              style={still ? undefined : { animation: "pwReveal .2s ease-out" }}
            >
              <span className="w-[30px] shrink-0 font-mono text-meta tracking-wide text-faint">TO</span>
              <div role="radiogroup" aria-label="Ombre, second colour" className="flex flex-wrap gap-2">
                {PROJECT_ACCENT_SWATCHES.map((swatch) => {
                  const same = swatch.hex === accent;
                  return (
                    <FillSwatch
                      key={swatch.hex}
                      name={same ? `${swatch.name}, same as the first colour` : swatch.name}
                      fill={projectFill({ accent, accent2: swatch.hex, pattern })}
                      on={accent2 === swatch.hex}
                      disabled={disabled || same}
                      dimmed={same}
                      onClick={() => chooseSecond(swatch.hex)}
                      onKeyDown={(e) =>
                        radioArrows(e, {
                          values: COLOURS,
                          current: accent2,
                          set: chooseSecond,
                          locked: disabled,
                          disabled: (hex) => hex === accent,
                        })
                      }
                    />
                  );
                })}
              </div>
            </div>
          )}
          <div className="flex items-baseline gap-2">
            <span id="pw-pattern" className={cn(LABEL, "mb-0")}>
              Pattern
            </span>
            <span
              data-testid="pw-pattern-name"
              aria-hidden="true"
              className="font-mono text-[10px] uppercase tracking-wide text-fg"
            >
              {PROJECT_PATTERNS.find((p) => p.id === pattern)?.name}
            </span>
          </div>
          <div role="radiogroup" aria-labelledby="pw-pattern" className="flex flex-wrap gap-2.5">
            {PROJECT_PATTERNS.map((option) => (
              <FillSwatch
                key={option.id}
                pattern
                name={option.name}
                fill={projectFill({ accent, accent2: sentAccent2, pattern: option.id })}
                on={pattern === option.id}
                disabled={disabled}
                onClick={() => choosePattern(option.id)}
                onKeyDown={(e) =>
                  radioArrows(e, {
                    values: PROJECT_PATTERNS.map((p) => p.id),
                    current: pattern,
                    set: choosePattern,
                    locked: disabled,
                  })
                }
              />
            ))}
          </div>
        </div>
        <div aria-hidden="true" className="flex shrink-0 flex-col items-center gap-1.5">
          <ProjectTile project={{ tag, accent, accent2: sentAccent2, pattern }} size={52} />
          <span className="font-mono text-meta tracking-wide text-faint">TILE</span>
        </div>
      </div>
    </div>
  );
}
