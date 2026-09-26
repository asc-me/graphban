/**
 * Shell chrome uses Plex Sans, not Plex Mono (GRPH-938).
 *
 * Mono belongs on identifiers — item ids, SHAs, enrolment codes, handles. Product phrases
 * in the chrome (subtitle, rail labels, loading text, sidebar labels) must be Sans so the
 * product does not read as a terminal. This test reads the source files directly so a
 * regression that re-adds `font-mono` to a chrome call site fails immediately.
 *
 * Bounce: walking FORWARD from the text line never sees `className=` on the parent
 * (it is on the line ABOVE). The enclosing element's className is found by walking
 * BACKWARD to the nearest `className=`.
 *
 * Source reads go through Vite `?raw` — `node:fs` is not in the web tsconfig.
 */
import { describe, expect, it } from "vitest";

const SHELL = import.meta.glob("../components/shell/{TopBar,LeftNav,AppFrame,AgentSidebar,PlaceHeader}.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

function readShell(name: string): string {
  const hit = Object.entries(SHELL).find(([k]) => k.endsWith(`/${name}`))?.[1];
  expect(hit, `${name} must be readable or these assertions mean nothing`).toBeTruthy();
  return hit as string;
}

/** className of the element that contains `marker` — parent line, not the text line. */
function enclosingClassName(src: string, marker: string, label: string): string {
  const lines = src.split("\n");
  const idx = lines.findIndex((l) => l.includes(marker));
  expect(idx, `${label}: marker "${marker}" not found in source`).toBeGreaterThanOrEqual(0);

  let classIdx = -1;
  for (let j = idx; j >= Math.max(0, idx - 8); j--) {
    if (lines[j]!.includes("className=")) {
      classIdx = j;
      break;
    }
  }
  expect(classIdx, `${label}: no className= above "${marker}"`).toBeGreaterThanOrEqual(0);

  let classText = lines[classIdx]!;
  let quoteCount = (classText.match(/"/g) || []).length;
  let walkIdx = classIdx;
  while (quoteCount % 2 !== 0 && walkIdx < lines.length - 1) {
    walkIdx++;
    classText += " " + lines[walkIdx]!;
    quoteCount = (classText.match(/"/g) || []).length;
  }
  return classText;
}

function assertLineNoMono(src: string, marker: string, label: string): void {
  expect(
    enclosingClassName(src, marker, label),
    `${label}: chrome must not use font-mono (GRPH-938)`,
  ).not.toContain("font-mono");
}

describe("shell chrome uses Sans, not Mono (GRPH-938)", () => {
  it("TopBar product subtitle is not font-mono", () => {
    const src = readShell("TopBar.tsx");
    assertLineNoMono(src, "Agent memory", "TopBar subtitle");
  });

  it("TopBar MCP live indicator is not font-mono", () => {
    const src = readShell("TopBar.tsx");
    assertLineNoMono(src, "tools live", "TopBar MCP indicator");
  });

  it("AppFrame loading text is not font-mono", () => {
    const src = readShell("AppFrame.tsx");
    assertLineNoMono(src, "loading…", "AppFrame loading");
  });

  it("LeftNav RailHeading is not font-mono", () => {
    const src = readShell("LeftNav.tsx");
    // The RailHeading component's className line.
    const lines = src.split("\n");
    const idx = lines.findIndex((l) => l.includes("RailHeading") && l.includes("function"));
    expect(idx, "RailHeading function not found").toBeGreaterThanOrEqual(0);
    // Find the className within the next few lines.
    let found = false;
    for (let j = idx!; j < Math.min(idx! + 10, lines.length); j++) {
      if (lines[j]!.includes("className=")) {
        expect(lines[j]!, "RailHeading: chrome must not use font-mono (GRPH-938)").not.toContain("font-mono");
        found = true;
        break;
      }
    }
    expect(found, "RailHeading className not found").toBe(true);
  });

  it("AgentSidebar memory count label is not font-mono", () => {
    const src = readShell("AgentSidebar.tsx");
    // `{countLabel}` is the render; `const countLabel` is the declaration and has no className.
    assertLineNoMono(src, "{countLabel}", "AgentSidebar count label");
  });

  it("AgentSidebar thinking indicator is not font-mono", () => {
    const src = readShell("AgentSidebar.tsx");
    assertLineNoMono(src, "thinking…", "AgentSidebar thinking");
  });

  it("PlaceHeader project name is not font-mono", () => {
    const src = readShell("PlaceHeader.tsx");
    assertLineNoMono(src, "{projectName}", "PlaceHeader project name");
  });

  it("identifiers still use font-mono (sabotage guard)", () => {
    // Handles, scores, and scope badges are identifiers and must stay mono. If this fails
    // someone removed mono from an identifier — the opposite regression.
    const topBar = readShell("TopBar.tsx");
    expect(topBar).toContain("font-mono"); // ⌘K and @handle still mono

    const sidebar = readShell("AgentSidebar.tsx");
    // Shard scores and scope badges remain mono.
    const lines = sidebar.split("\n");
    const scoreLine = lines.find((l) => l.includes("s.score.toFixed"));
    expect(scoreLine, "shard score line should still use font-mono").toContain("font-mono");
  });
});
