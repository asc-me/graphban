import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * GRPH-1091. Vitest 3.2.7 still declared tinypool ^1.1.1, which is inside
 * both tinypool RCE ranges, so a pnpm override kept the install on >=2.1.2.
 * Vitest 4.1.12 is the first release outside the @vitest/mocker path-traversal
 * ranges, and it no longer depends on tinypool at all; the override stays as a
 * guard in case something brings tinypool back. Sabotage: point the vitest
 * importer or the @vitest/mocker entry at 3.2.7, or put the override or a
 * tinypool entry on 1.1.1 — these assertions must fail.
 */

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");

function atLeast(version: string, floor: string): boolean {
  const parts = (value: string) => value.split(".").map((part) => Number(part));
  const left = parts(version);
  const right = parts(floor);
  for (let i = 0; i < 3; i++) {
    const delta = (left[i] ?? 0) - (right[i] ?? 0);
    if (delta !== 0) return delta > 0;
  }
  return true;
}

describe("vitest and tinypool floors (GRPH-1091)", () => {
  it("resolves vitest >= 4.1.12", () => {
    const lock = readFileSync(join(webRoot, "pnpm-lock.yaml"), "utf8");
    const importer = lock.match(
      /^ {6}vitest:\n {8}specifier: \S+\n {8}version: (\d+\.\d+\.\d+)/m,
    );
    expect(importer, "vitest importer missing from the lockfile").not.toBeNull();
    expect(atLeast(importer![1]!, "4.1.12")).toBe(true);
  });

  it("resolves every @vitest/mocker >= 4.1.12", () => {
    const lock = readFileSync(join(webRoot, "pnpm-lock.yaml"), "utf8");
    const published = [...lock.matchAll(/^ {2}'@vitest\/mocker@(\d+\.\d+\.\d+)/gm)].map((match) => match[1]!);
    expect(published.length, "@vitest/mocker missing from the lockfile").toBeGreaterThan(0);
    for (const version of published) expect(atLeast(version, "4.1.12")).toBe(true);
  });

  it("keeps any tinypool that resolves >= 2.1.2", () => {
    const pkg = JSON.parse(readFileSync(join(webRoot, "package.json"), "utf8")) as {
      pnpm?: { overrides?: { tinypool?: string } };
    };
    const override = pkg.pnpm?.overrides?.tinypool ?? "";
    expect(atLeast(override, "2.1.2")).toBe(true);

    const lock = readFileSync(join(webRoot, "pnpm-lock.yaml"), "utf8");
    // Vitest 4 resolves no tinypool, so an empty list is the expected state
    // here, not a missed match: the floor applies to whatever does resolve.
    const published = [...lock.matchAll(/^ {2}tinypool@(\d+\.\d+\.\d+):/gm)].map((match) => match[1]!);
    for (const version of published) expect(atLeast(version, "2.1.2")).toBe(true);

    const pins = [...lock.matchAll(/\n {6}tinypool: (\d+\.\d+\.\d+)/g)].map((match) => match[1]!);
    for (const version of pins) expect(atLeast(version, "2.1.2")).toBe(true);
  });
});
