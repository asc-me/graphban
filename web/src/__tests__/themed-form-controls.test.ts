import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "index.css"), "utf8");
const base = css.slice(css.indexOf("@layer base"), css.indexOf("/* Updates panel motion"));
const R = 'input[type="radio"]:not(.sr-only):not(.appearance-none)';
const C = 'input[type="checkbox"]:not(.sr-only):not(.appearance-none)';

describe("themed native radios and checkboxes (GRPH-1754)", () => {
  it("lives in the base layer and resets appearance", () => {
    expect(base).toContain("appearance: none");
  });
  it("styles radio unchecked and checked", () => {
    expect(base).toContain(`:where(${R}) { border-radius: 999px; }`);
    expect(base).toContain(`:where(${R}):checked`);
    expect(base).toContain("radial-gradient");
  });
  it("styles checkbox unchecked, checked and indeterminate", () => {
    expect(base).toContain(`:where(${C}) { border-radius: 3px; }`);
    expect(base).toContain(`:where(${C}):checked`);
    expect(base).toContain(`:where(${C}):indeterminate`);
  });
  it("dims disabled controls", () => {
    expect(base).toMatch(/:disabled,[\s\S]*?:disabled \{\s*opacity: \.45;\s*cursor: not-allowed/);
  });
  it("excludes sr-only and appearance-none inputs", () => {
    expect(base).toContain(":not(.sr-only):not(.appearance-none)");
  });
  it("does not remove the focus-visible outline", () => {
    expect(css).toContain(":focus-visible");
  });
});
