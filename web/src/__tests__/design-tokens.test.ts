import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const CSS = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../index.css"), "utf8");

/** Parsed from index.css @theme — sabotage reverts there must fail these assertions. */
export const TOKENS = parseThemeTokens(CSS);

export function parseThemeTokens(css: string): Record<string, string> {
  const tokens: Record<string, string> = {};
  for (const m of css.matchAll(/--([a-z0-9-]+):\s*([^;]+);/gi)) {
    tokens[m[1]!] = m[2]!.trim();
  }
  return tokens;
}

function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

function relativeLuminance([r, g, b]: [number, number, number]): number {
  const ch = (x: number) => {
    const s = x / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b);
}

/** WCAG 2.2 contrast ratio between two sRGB hex colours. */
export function contrastRatio(fgHex: string, bgHex: string): number {
  const l1 = relativeLuminance(hexToRgb(fgHex));
  const l2 = relativeLuminance(hexToRgb(bgHex));
  const lighter = Math.max(l1, l2);
  const darker = Math.min(l1, l2);
  return (lighter + 0.05) / (darker + 0.05);
}

describe("design token foundation (GRPH-908)", () => {
  it("raises faint copy above 4.5:1 on bg and surface-3", () => {
    const faint = TOKENS["color-faint"]!;
    expect(contrastRatio(faint, TOKENS["color-bg"]!)).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(faint, TOKENS["color-surface-3"]!)).toBeGreaterThanOrEqual(4.5);
  });

  // faint-2 is the dimmer of the two faint roles. On the design's surface ramp there is no
  // value both dimmer than faint and above 4.5:1 on surface-3 — luminance decides contrast,
  // so "dimmer" and "passes on the lightest surface" are the same axis. It therefore holds
  // the floor on the surfaces it is actually used on, and a source scan below keeps it there.
  it("raises faint-2 copy above 4.5:1 on every surface it is used on", () => {
    const faint2 = TOKENS["color-faint-2"]!;
    for (const key of ["color-bg", "color-surface", "color-surface-2"] as const) {
      expect(contrastRatio(faint2, TOKENS[key]!)).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("sabotage: legacy faint #5c656e fails the login/tagline floor", () => {
    expect(contrastRatio("#5c656e", TOKENS["color-bg"]!)).toBeLessThan(4.5);
    expect(contrastRatio(TOKENS["color-faint"]!, TOKENS["color-bg"]!)).toBeGreaterThanOrEqual(4.5);
  });

  it("control borders meet 3:1 on every surface a control sits on", () => {
    for (const key of ["color-control", "color-control-hover"] as const) {
      for (const surf of ["color-surface", "color-surface-2", "color-surface-3"] as const) {
        expect(contrastRatio(TOKENS[key]!, TOKENS[surf]!)).toBeGreaterThanOrEqual(3);
      }
    }
  });

  // The counterpart of the rule above, and the reason this split exists. A panel edge is
  // decorative under WCAG 1.4.11; raising it to the control floor is what outlined every
  // card in the app in a 3.5:1 grey. If a later pass brightens these, this fails.
  it("keeps hairlines below the control floor — structure is not a control", () => {
    const s2 = TOKENS["color-surface-2"]!;
    for (const key of ["color-line", "color-line-2", "color-line-hover", "color-line-3"] as const) {
      expect(contrastRatio(TOKENS[key]!, s2)).toBeLessThan(2);
    }
  });

  it("pins the hairlines to the design's own values", () => {
    expect(TOKENS["color-line"]).toBe("#2a333a");
    expect(TOKENS["color-line-2"]).toBe("#1e242a");
    expect(TOKENS["color-line-hover"]).toBe("#2f3a42");
    expect(TOKENS["color-line-3"]).toBe("#3a444c");
  });

  it("focus accent meets 3:1 on every surface it lands on", () => {
    const focus = TOKENS["color-focus"]!;
    for (const key of ["color-bg", "color-surface-2", "color-surface-3"] as const) {
      expect(contrastRatio(focus, TOKENS[key]!)).toBeGreaterThanOrEqual(3);
    }
  });

  it("login surfaces meet contrast floors (tagline, labels, input border, focus ring)", () => {
    const faint = TOKENS["color-faint"]!;
    const bg = TOKENS["color-bg"]!;
    const s3 = TOKENS["color-surface-3"]!;
    const s2 = TOKENS["color-surface-2"]!;
    const focus = TOKENS["color-focus"]!;

    // Tagline: font-mono text-faint on page bg
    expect(contrastRatio(faint, bg)).toBeGreaterThanOrEqual(4.5);
    // Field labels: uppercase mono text-faint on form surface-3
    expect(contrastRatio(faint, s3)).toBeGreaterThanOrEqual(4.5);
    // Input default border: border-control on bg-surface-2
    expect(contrastRatio(TOKENS["color-control"]!, s2)).toBeGreaterThanOrEqual(3);
    // Input hover border: border-control-hover on bg-surface-2
    expect(contrastRatio(TOKENS["color-control-hover"]!, s2)).toBeGreaterThanOrEqual(3);
    // Focus ring: outline color-focus on surfaces behind inputs
    for (const surface of [bg, s2, s3] as const) {
      expect(contrastRatio(focus, surface)).toBeGreaterThanOrEqual(3);
    }
  });

  it("does not reuse line-hover as the focus token", () => {
    expect(TOKENS["color-focus"]).not.toBe(TOKENS["color-line-hover"]);
  });

  it("keeps faint's floor on surface-3, the lightest surface it lands on", () => {
    expect(contrastRatio(TOKENS["color-faint"]!, TOKENS["color-surface-3"]!)).toBeGreaterThanOrEqual(4.5);
  });

  it("defines a collapsed type scale (≤7 steps, ≥1.2× apart)", () => {
    const roles = [
      "text-micro",
      "text-meta",
      "text-secondary",
      "text-body",
      "text-title",
      "text-display",
    ] as const;
    const sizes = roles.map((r) => parseFloat(TOKENS[r]!) * 16);
    expect(sizes.length).toBeLessThanOrEqual(7);
    for (let i = 1; i < sizes.length; i++) {
      expect(sizes[i]! / sizes[i - 1]!).toBeGreaterThanOrEqual(1.2);
    }
  });

  it("defines three radius steps", () => {
    expect(TOKENS["radius-control"]).toBeTruthy();
    expect(TOKENS["radius-card"]).toBeTruthy();
    expect(TOKENS["radius-overlay"]).toBeTruthy();
  });

  it("elevation shadows do not draw a 1px stroke via --color-line (GRPH-937)", () => {
    for (const key of ["shadow-elev-1", "shadow-elev-2", "shadow-elev-3"] as const) {
      expect(TOKENS[key]).not.toContain("--color-line");
    }
  });

  it("sabotage: restoring 0 0 0 1px var(--color-line) on shadow-elev-1 must fail", () => {
    const shadow = TOKENS["shadow-elev-1"]!;
    expect(shadow).not.toContain("0 0 0 1px var(--color-line)");
    expect(shadow).not.toContain("--color-line");
  });

  it("page has a film-grain overlay (SVG noise on body) (GRPH-937)", () => {
    expect(CSS).toMatch(/body::before\s*\{[^}]*url\(/s);
  });
});
