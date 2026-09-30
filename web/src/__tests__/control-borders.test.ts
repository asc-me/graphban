import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..");

function tsxFiles(dir: string, acc: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) tsxFiles(p, acc);
    else if (p.endsWith(".tsx")) acc.push(p);
  }
  return acc;
}

/** The opening tag of every <button|input|textarea|select>, quotes and {…} balanced. */
function controlTags(src: string): string[] {
  const out: string[] = [];
  const re = /<(button|input|textarea|select)\b/gi;
  for (let m = re.exec(src); m; m = re.exec(src)) {
    let i = m.index + m[0].length;
    let depth = 0;
    let quote: string | null = null;
    for (; i < src.length; i++) {
      const c = src[i]!;
      if (quote) {
        if (c === quote) quote = null;
        else if (c === "\\") i++;
      } else if (c === '"' || c === "'" || c === "`") quote = c;
      else if (c === "{") depth++;
      else if (c === "}") depth--;
      else if (c === ">" && depth === 0) break;
    }
    out.push(src.slice(m.index, i));
  }
  return out;
}

const FILES = tsxFiles(SRC);

describe("control borders stay off the hairline tokens", () => {
  // The hairlines are drawn at ~1.2:1 so panels stop looking outlined. A control's border
  // IS its identity under WCAG 1.4.11, so controls take --color-control instead. Putting a
  // control back on a hairline is invisible in review and silently drops it below 3:1.
  it("no button, input, textarea or select carries a border-line class", () => {
    const offenders: string[] = [];
    for (const f of FILES) {
      for (const tag of controlTags(readFileSync(f, "utf8"))) {
        if (/\bborder-line(-[a-z0-9]+)?\b/.test(tag)) {
          offenders.push(`${f.slice(SRC.length + 1)} :: ${tag.replace(/\s+/g, " ").slice(0, 100)}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  // A tag scan cannot see a class string built in a constant or a cva variant map, which is
  // exactly where the first pass of this change left three controls on hairlines. These are
  // the shared definitions every control inherits, so they are asserted by name.
  it("the shared control definitions use the control tokens", () => {
    const input = readFileSync(join(SRC, "components/ui/input.tsx"), "utf8");
    expect(input).toMatch(/border border-control\b/);
    expect(input).not.toMatch(/\bborder-line/);

    const button = readFileSync(join(SRC, "components/ui/button.tsx"), "utf8");
    const outline = /outline:\s*([\s\S]*?),\n\s{8}ghost:/.exec(button)?.[1] ?? "";
    expect(outline).toMatch(/border-control\b/);
    expect(outline).not.toMatch(/\bborder-line/);
  });
});

describe("text tokens stay on surfaces they can carry", () => {
  // faint-2 clears 4.5:1 on bg, surface and surface-2 but not on the lighter surface-3/4.
  // Pairing it with those is a contrast failure a reviewer cannot see, so it is refused here.
  it("text-faint-2 is never placed on surface-3 or surface-4", () => {
    const offenders: string[] = [];
    const attr = /className=(?:"([^"]*)"|\{`([^`]*)`\})/gs;
    for (const f of FILES) {
      const src = readFileSync(f, "utf8");
      for (let m = attr.exec(src); m; m = attr.exec(src)) {
        const cls = m[1] ?? m[2] ?? "";
        if (/\btext-faint-2\b/.test(cls) && /\bbg-surface-[34]\b/.test(cls)) {
          offenders.push(`${f.slice(SRC.length + 1)} :: ${cls.slice(0, 90)}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
