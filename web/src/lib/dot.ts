import { projectFill } from "@/lib/projectFill";
import type { ProjectPattern, User } from "@/lib/types";

/**
 * The one dot renderer (GRPH-1792): (colour, colour2, pattern) → CSS fill.
 *
 * Same pattern ids and same layers as project tiles (`TILE_PATTERNS`, GRPH-1746) — this
 * delegates to `projectFill`, so a project tile can adopt it later without a second table.
 * A missing `colour2` or `pattern` draws a solid fill.
 */
export interface DotInput {
  colour: string;
  colour2?: string | null;
  pattern?: ProjectPattern | null;
}

export function dotFill({ colour, colour2, pattern }: DotInput): string {
  return projectFill({ accent: colour, accent2: colour2 ?? null, pattern: pattern ?? "none" });
}

/** The letters keep a glow in the first colour so they read over a pattern. */
export function dotGlow(colour: string, pattern?: ProjectPattern | null): string | undefined {
  return pattern && pattern !== "none" ? `0 0 3px ${colour}, 0 0 6px ${colour}` : undefined;
}

export function userDot(u: Pick<User, "avatar" | "avatar2" | "avatar_pattern">): DotInput {
  return { colour: u.avatar, colour2: u.avatar2 ?? null, pattern: u.avatar_pattern ?? "none" };
}

/** The 12 colours the dot picker offers. */
export const DOT_COLOURS = [
  "#c6f24e", "#5fd07a", "#2dd4bf", "#38bdf8", "#6b8afd", "#a78bfa",
  "#e879f9", "#fb7185", "#f97316", "#fbbf24", "#8b949e", "#e5e7eb",
] as const;
