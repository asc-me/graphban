import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { TYPE_ROLES } from "./typeScale";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..");

function tsxFiles(dir: string, acc: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) tsxFiles(p, acc);
    else if (p.endsWith(".tsx")) acc.push(p);
  }
  return acc;
}

const SOURCES = tsxFiles(SRC).map((f) => readFileSync(f, "utf8"));

describe("the type scale has consumers", () => {
  // THE regression. The previous scale shipped with a test that it was well formed and no
  // test that anything used it, so six tokens sat unread while 677 call sites set the same
  // sizes by hand. A scale nothing consumes is not a scale; it is a comment (GRPH-1004).
  it("every role is used by at least one component", () => {
    const unused = TYPE_ROLES.filter(
      (role) => !SOURCES.some((src) => new RegExp(`\\b${role}\\b`).test(src)),
    );
    expect(unused).toEqual([]);
  });

  // The roled sizes must not creep back in as arbitrary values, or the scale decays into a
  // suggestion. Sizes with no role are deliberately left alone — see index.css.
  it("no component sets a roled size as an arbitrary value", () => {
    const roled = ["8.5px", "9.5px", "11px", "12.5px", "14px", "20px"];
    const offenders: string[] = [];
    for (const px of roled) {
      const hits = SOURCES.filter((src) => src.includes(`text-[${px}]`)).length;
      if (hits) offenders.push(`text-[${px}] in ${hits} file(s)`);
    }
    expect(offenders).toEqual([]);
  });
});
