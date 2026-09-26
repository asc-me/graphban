/**
 * Shell chrome uses Plex Sans, not Plex Mono (GRPH-938).
 *
 * Mono belongs on identifiers — item ids, SHAs, enrolment codes, handles. Product phrases
 * in the chrome (subtitle, rail labels, loading text, sidebar labels) must be Sans so the
 * product does not read as a terminal. This test reads the source files directly so a
 * regression that re-adds `font-mono` to a chrome call site fails immediately.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SHELL_DIR = join(
  dirname(fileURLToPath(import.meta.url)),
  "../components/shell",
);

function readShell(name: string): string {
  return readFileSync(join(SHELL_DIR, name), "utf8");
}

/**
 * Extract the className string on the line containing `marker`, then check it does NOT
 * include `font-mono`. Returns the className for assertion messages.
 */
function assertLineNoMono(src: string, marker: string, label: string): void {
  const lines = src.split("\n");
  const idx = lines.findIndex((l) => l.includes(marker));
  expect(idx, `${label}: marker "${marker}" not found in source`).toBeGreaterThanOrEqual(0);

  // Collect the full className by joining lines from the className= line until the closing
  // quote. A single-line className is the common case; multi-line JSX strings are handled
  // by concatenating until we see the closing `"`.
  let line = lines[idx]!;
  const classStart = line.indexOf("className=");
  if (classStart === -1) {
    // The marker is on a line before className=; walk forward to find it.
    for (let j = idx; j < Math.min(idx + 5, lines.length); j++) {
      if (lines[j]!.includes("className=")) {
        line = lines[j]!;
        break;
      }
    }
  }
  // Walk forward to close the string if it spans multiple lines.
  let classText = line;
  let quoteCount = (classText.match(/"/g) || []).length;
  let walkIdx = lines.indexOf(line);
  while (quoteCount % 2 !== 0 && walkIdx < lines.length - 1) {
    walkIdx++;
    classText += " " + lines[walkIdx]!;
    quoteCount = (classText.match(/"/g) || []).length;
  }
  expect(classText, `${label}: chrome must not use font-mono (GRPH-938)`).not.toContain("font-mono");
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
    assertLineNoMono(src, "countLabel", "AgentSidebar count label");
  });

  it("AgentSidebar thinking indicator is not font-mono", () => {
    const src = readShell("AgentSidebar.tsx");
    assertLineNoMono(src, "thinking…", "AgentSidebar thinking");
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
