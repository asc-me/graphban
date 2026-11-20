import type { CSSProperties } from "react";

import { cn } from "@/lib/cn";
import { projectFill } from "@/lib/projectFill";
import type { Project } from "@/lib/types";

/**
 * The project tile (GRPH-1748). One square, one fill.
 *
 * The tag is dark with a neutral glow. An accent-coloured glow reads as a halo
 * of the first colour over the second half of an ombre, so the glow stays white.
 * The rail still asks for a 30px tile (GRPH-1208); other sizes pass `size`.
 */

export const TILE_GLOW = "0 0 3px rgba(255,255,255,.55), 0 0 6px rgba(255,255,255,.35)";
export const TILE_INK = "#0a0b0d";

/** What the fill reads. A missing project still draws the default accent, not a blank. */
export type ProjectTileSource = Pick<Project, "tag" | "accent" | "accent2" | "pattern"> | null;

const DEFAULT_SIZE = 30;
const DEFAULT_ACCENT = "#c6f24e";

export function ProjectTile({
  project,
  size = DEFAULT_SIZE,
}: {
  project: ProjectTileSource;
  size?: number;
}) {
  const accent = project?.accent || DEFAULT_ACCENT;
  const fill = projectFill({
    accent,
    accent2: project?.accent2,
    pattern: project?.pattern,
  });
  const tag = project?.tag?.trim() || "—";
  // The fill is a custom property because a multi-layer background (an SVG
  // pattern over a gradient) is what the tile paints. jsdom's CSS parser drops
  // that shorthand, so tests read `--tile-fill`. A browser paints the var.
  const style = {
    "--tile-fill": fill,
    background: "var(--tile-fill)",
    color: TILE_INK,
    textShadow: TILE_GLOW,
    width: size,
    height: size,
    borderRadius: Math.round((size * 9) / 32),
    fontSize: Math.min(12, Math.max(8, Math.round((size * 11) / 32))),
  } as CSSProperties;
  return (
    <span
      data-testid="project-tile"
      aria-hidden
      className={cn(
        "flex flex-none items-center justify-center overflow-hidden whitespace-nowrap font-mono font-bold",
        size === DEFAULT_SIZE && "h-[30px] w-[30px]",
      )}
      style={style}
    >
      {tag}
    </span>
  );
}
