import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "index.css"), "utf8");
const base = css.slice(css.indexOf("@layer base"), css.indexOf("/* Updates panel motion"));
const R = 'input:where([type="radio"]:not(.sr-only):not(.appearance-none))';
const C = 'input:where([type="checkbox"]:not(.sr-only):not(.appearance-none))';

describe("themed native radios and checkboxes (GRPH-1754)", () => {
  it("lives in the base layer and resets appearance", () => {
    expect(base).toContain("appearance: none");
  });
  it("styles radio unchecked and checked", () => {
    expect(base).toContain(`${R} { border-radius: 999px; }`);
    expect(base).toContain(`${R}:checked`);
    expect(base).toContain("radial-gradient");
  });
  it("styles checkbox unchecked, checked and indeterminate", () => {
    expect(base).toContain(`${C} { border-radius: 3px; }`);
    expect(base).toContain(`${C}:checked`);
    expect(base).toContain(`${C}:indeterminate`);
  });
  it("dims disabled controls", () => {
    expect(base).toMatch(/:disabled,[\s\S]*?:disabled \{\s*opacity: \.45;\s*cursor: not-allowed/);
  });
  it("keeps element specificity so Tailwind preflight (input { border-radius: 0 }) does not win", () => {
    // A bare :where(input...) is specificity 0,0,0 and loses to preflight in the
    // same layer — radios rendered square (GRPH-1754 bounce).
    expect(base).not.toContain(":where(input");
  });
  it("excludes sr-only and appearance-none inputs", () => {
    expect(base).toContain(":not(.sr-only):not(.appearance-none)");
  });
  it("does not remove the focus-visible outline", () => {
    expect(css).toContain(":focus-visible");
  });
});
