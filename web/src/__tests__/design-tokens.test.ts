import { describe, expect, it } from "vitest";

/**
 * GRPH-908 contrast contract. When index.css @theme changes, update these literals —
 * they are the acceptance floor the sabotage test pins.
 */
const TOKENS = {
  "color-bg": "#0a0c0e",
  "color-surface-2": "#101418",
  "color-surface-3": "#12171b",
  "color-faint": "#7f8891",
  "color-line": "#5a636c",
  "color-line-2": "#646d76",
  "color-line-hover": "#6e7780",
  "color-focus": "#c6f24e",
  "text-micro": "0.625rem",
  "text-meta": "0.75rem",
  "text-secondary": "0.9375rem",
  "text-body": "1.125rem",
  "text-title": "1.375rem",
  "text-display": "1.6875rem",
  "radius-control": "0.5rem",
  "radius-card": "0.75rem",
  "radius-overlay": "1rem",
} as const;

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
    const faint = TOKENS["color-faint"];
    expect(contrastRatio(faint, TOKENS["color-bg"])).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(faint, TOKENS["color-surface-3"])).toBeGreaterThanOrEqual(4.5);
  });

  it("sabotage: legacy faint #5c656e fails the login/tagline floor", () => {
    expect(contrastRatio("#5c656e", TOKENS["color-bg"])).toBeLessThan(4.5);
  });

  it("input borders meet 3:1 on surface-2", () => {
    const bg = TOKENS["color-surface-2"];
    for (const key of ["color-line", "color-line-2", "color-line-hover"] as const) {
      expect(contrastRatio(TOKENS[key], bg)).toBeGreaterThanOrEqual(3);
    }
  });

  it("focus accent meets 3:1 on every surface it lands on", () => {
    const focus = TOKENS["color-focus"];
    for (const key of ["color-bg", "color-surface-2", "color-surface-3"] as const) {
      expect(contrastRatio(focus, TOKENS[key])).toBeGreaterThanOrEqual(3);
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
    const sizes = roles.map((r) => parseFloat(TOKENS[r]) * 16);
    expect(sizes.length).toBeLessThanOrEqual(7);
    for (let i = 1; i < sizes.length; i++) {
      expect(sizes[i] / sizes[i - 1]).toBeGreaterThanOrEqual(1.2);
    }
  });

  it("defines three radius steps", () => {
    expect(TOKENS["radius-control"]).toBeTruthy();
    expect(TOKENS["radius-card"]).toBeTruthy();
    expect(TOKENS["radius-overlay"]).toBeTruthy();
  });
});
