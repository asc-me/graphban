import { describe, expect, it } from "vitest";

/**
 * PRD-52 D8. The within-repo galaxy collapse is gone. A future edit that
 * imports `collapse`, `enterComponent`, `superRadius` or `worthCollapsing`
 * from `lib/graph/galaxy` is the regression this file exists to catch.
 *
 * The assertion reads source. A render test stays green when the import is
 * added and never called; this one does not. Comments that name the symbols
 * are not imports.
 */
const REMOVED = new Set(["collapse", "enterComponent", "superRadius", "worthCollapsing"]);

const NAMED_FROM = /(?:import|export)\s+(?:type\s+)?\{([^}]*)\}\s*from\s*["']([^"']+)["']/g;

/** Drop comments so a commented-out import, or prose that quotes one, is not a hit. */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:\\])\/\/[^\n]*/g, "$1");
}

/**
 * True when `spec`, written from the file at `importer`, names `lib/graph/galaxy`.
 * `importer` is a `import.meta.glob` key relative to this test (`../…`).
 */
function resolvesToGalaxy(importer: string, spec: string): boolean {
  const bare = spec.replace(/\.tsx?$/, "");
  let rel: string;
  if (bare.startsWith("@/")) {
    rel = bare.slice(2);
  } else if (bare.startsWith(".")) {
    const stack = importer.split("/").slice(0, -1);
    for (const part of bare.split("/")) {
      if (part === "." || part === "") continue;
      if (part === "..") stack.pop();
      else stack.push(part);
    }
    rel = stack.join("/").replace(/^\.\.\//, "");
  } else {
    return false;
  }
  return rel === "lib/graph/galaxy";
}

function importedNames(clause: string): string[] {
  const names: string[] = [];
  for (const part of clause.split(",")) {
    const match = part.trim().match(/^(?:type\s+)?([A-Za-z_$][\w$]*)/);
    if (match) names.push(match[1]);
  }
  return names;
}

function removedGalaxyImports(importer: string, source: string): string[] {
  const hits: string[] = [];
  const named = new RegExp(NAMED_FROM.source, "g");
  for (const match of stripComments(source).matchAll(named)) {
    if (!resolvesToGalaxy(importer, match[2])) continue;
    for (const name of importedNames(match[1])) {
      if (REMOVED.has(name)) hits.push(name);
    }
  }
  return hits;
}

describe("galaxy import ratchet (PRD-52 D8)", () => {
  it("imports none of the removed collapse symbols from lib/graph/galaxy", () => {
    const sources = import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;

    const offenders: string[] = [];
    for (const [path, text] of Object.entries(sources)) {
      for (const name of removedGalaxyImports(path, text)) {
        offenders.push(`${path}: ${name}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
