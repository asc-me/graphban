import type { ProjectPattern } from "@/lib/types";

/**
 * One project-tile fill (GRPH-1748).
 *
 * Ported from the project-wizard design (2026-11-19): a dark ink laid over the
 * accent, or over a two-colour ombre, so every pattern reads on every accent.
 * `accent` alone stays the solid colour for text, borders, dots and graph nodes —
 * this value is only the tile's `background`.
 */

/** Dark ink laid over the base. The same ink on every accent. */
const INK = "rgba(10,11,13,.34)";

export interface ProjectFillInput {
  accent: string;
  accent2?: string | null;
  pattern?: ProjectPattern | null;
}

/** Id plus the name the wizard shows. Order is the design's order. */
export interface ProjectPatternOption {
  id: ProjectPattern;
  name: string;
}

function svgPattern(w: number, h: number, d: string): string {
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}">` +
    `<path d="${d}" fill="none" stroke="${INK}" stroke-width="1.4" stroke-linecap="round"/></svg>`;
  return `url('data:image/svg+xml,${encodeURIComponent(svg)}')`;
}

function ring(cx: number, cy: number, r: number): string {
  return `M${cx - r} ${cy}a${r} ${r} 0 1 0 ${2 * r} 0a${r} ${r} 0 1 0 ${-2 * r} 0`;
}

/** One paisley: a teardrop with a hooked tip and an eye. `flip` mirrors it, `k` scales it. */
function paisley(x: number, y: number, flip: boolean, k: number): string {
  const s = (flip ? -1 : 1) * k;
  const q = k;
  return (
    `M${x} ${y + 7 * q}` +
    `c${-3 * s} 0 ${-4.5 * s} ${-2.2 * q} ${-4.5 * s} ${-4.6 * q}` +
    `c0 ${-3 * q} ${3 * s} ${-5.2 * q} ${6.2 * s} ${-5.6 * q}` +
    `c${-1.6 * s} ${1.2 * q} ${-1.4 * s} ${2.6 * q} ${0.2 * s} ${3.4 * q}` +
    `c${1.8 * s} ${0.9 * q} ${2.4 * s} ${2.8 * q} ${1.6 * s} ${4.4 * q}` +
    `c${-0.8 * s} ${1.6 * q} ${-2.2 * s} ${2.4 * q} ${-3.5 * s} ${2.4 * q}z` +
    ring(x + (flip ? 0.6 : -0.6) * k, y + 2.6 * k, 1.1 * k)
  );
}

/**
 * The design draws paisley at k=1.6 on a 32px tile. That tile is taller than a
 * 28px swatch, and the tip sits above y=0, so the motif is clipped. k=1 on a
 * 22px tile keeps a whole paisley inside the repeat, and 22 fits in 28.
 */
const PAISLEY_TILE = 22;
const PAISLEY_K = 1;

const LAYERS: { id: ProjectPattern; name: string; layer: string }[] = [
  { id: "none", name: "None", layer: "" },
  {
    id: "speckled",
    name: "Speckled",
    layer:
      `radial-gradient(${INK} 0.9px, transparent 1.3px) 0 0/6px 6px, ` +
      `radial-gradient(${INK} 0.9px, transparent 1.3px) 3px 3px/6px 6px`,
  },
  {
    id: "crosshatch",
    name: "Cross hatch",
    layer:
      `repeating-linear-gradient(45deg, ${INK} 0 1px, transparent 1px 5px), ` +
      `repeating-linear-gradient(-45deg, ${INK} 0 1px, transparent 1px 5px)`,
  },
  { id: "chevron", name: "Chevron", layer: svgPattern(12, 8, "M0 7L6 1L12 7") },
  {
    id: "herringbone",
    name: "Herringbone",
    layer: svgPattern(16, 12, "M0 2L8 -6M0 8L8 0M0 14L8 6M8 -3L16 5M8 3L16 11M8 9L16 17"),
  },
  {
    id: "stripes",
    name: "Diagonal stripes",
    layer: `repeating-linear-gradient(45deg, ${INK} 0 2.5px, transparent 2.5px 7px)`,
  },
  {
    id: "paisley",
    name: "Paisley",
    layer: svgPattern(
      PAISLEY_TILE,
      PAISLEY_TILE,
      paisley(7, 4, false, PAISLEY_K) + paisley(16, 12, true, PAISLEY_K),
    ),
  },
  {
    id: "bubbles",
    name: "Bubbles",
    layer: svgPattern(22, 22, ring(5, 6, 3.2) + ring(15, 4, 1.6) + ring(14, 15, 4.6) + ring(4, 17, 1.8)),
  },
];

export const PROJECT_PATTERNS: ProjectPatternOption[] = LAYERS.map(({ id, name }) => ({ id, name }));

function sameColour(a: string, b: string): boolean {
  return a.trim().toLowerCase() === b.trim().toLowerCase();
}

/** CSS `background`: the pattern layer (if any) over a solid accent or a 135° ombre. */
export function projectFill({ accent, accent2, pattern }: ProjectFillInput): string {
  const second = accent2 && !sameColour(accent2, accent) ? accent2.trim() : null;
  const base = second ? `linear-gradient(135deg, ${accent} 12%, ${second} 88%)` : accent;
  const layer = LAYERS.find((p) => p.id === pattern)?.layer ?? "";
  return layer ? `${layer}, ${base}` : base;
}
