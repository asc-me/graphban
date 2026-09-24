import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/** Parsed from index.css @theme — sabotage reverts there must fail these assertions. */
export const TOKENS = parseThemeTokens(
  readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../index.css"), "utf8"),
);

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

  it("raises faint-2 copy above 4.5:1 on bg and surface-3", () => {
    const faint2 = TOKENS["color-faint-2"]!;
    expect(contrastRatio(faint2, TOKENS["color-bg"]!)).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(faint2, TOKENS["color-surface-3"]!)).toBeGreaterThanOrEqual(4.5);
  });

  it("sabotage: legacy faint #5c656e fails the login/tagline floor", () => {
    expect(contrastRatio("#5c656e", TOKENS["color-bg"]!)).toBeLessThan(4.5);
    expect(contrastRatio(TOKENS["color-faint"]!, TOKENS["color-bg"]!)).toBeGreaterThanOrEqual(4.5);
  });

  it("input borders meet 3:1 on surface-2", () => {
    const bg = TOKENS["color-surface-2"]!;
    for (const key of ["color-line", "color-line-2", "color-line-hover"] as const) {
      expect(contrastRatio(TOKENS[key]!, bg)).toBeGreaterThanOrEqual(3);
    }
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
    // Input default border: border-line-2 on bg-surface-2
    expect(contrastRatio(TOKENS["color-line-2"]!, s2)).toBeGreaterThanOrEqual(3);
    // Input hover border: border-line-hover on bg-surface-2
    expect(contrastRatio(TOKENS["color-line-hover"]!, s2)).toBeGreaterThanOrEqual(3);
    // Focus ring: outline color-focus on surfaces behind inputs
    for (const surface of [bg, s2, s3] as const) {
      expect(contrastRatio(focus, surface)).toBeGreaterThanOrEqual(3);
    }
  });

  it("does not reuse line-hover as the focus token", () => {
    expect(TOKENS["color-focus"]).not.toBe(TOKENS["color-line-hover"]);
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
});
