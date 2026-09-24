// @ts-nocheck — reads Node fs at runtime; vitest runs in Node. CSS ?raw is emptied by the Vite CSS plugin.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import agentSidebarSrc from "../components/shell/AgentSidebar.tsx?raw";
import appFrameSrc from "../components/shell/AppFrame.tsx?raw";
import dialogSrc from "../components/ui/dialog.tsx?raw";
import dropdownSrc from "../components/ui/dropdown-menu.tsx?raw";
import docsSrc from "../features/docs/DocsReader.tsx?raw";

/**
 * GRPH-909 motion-system contract. These tests read the source on purpose so sabotage
 * (delete the reduced-motion block; force 240ms on the keyboard Agent path) fails the suite.
 */

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const cssSrc = readFileSync(resolve(root, "index.css"), "utf8");

describe("motion system (GRPH-909)", () => {
  it("defines the named easing and duration tokens", () => {
    expect(cssSrc).toMatch(/--ease-out:\s*cubic-bezier\(0\.23,\s*1,\s*0\.32,\s*1\)/);
    expect(cssSrc).toMatch(/--ease-in-out:\s*cubic-bezier\(0\.77,\s*0,\s*0\.175,\s*1\)/);
    expect(cssSrc).toMatch(/--duration-press:\s*120ms/);
    expect(cssSrc).toMatch(/--duration-ui:\s*180ms/);
    expect(cssSrc).toMatch(/--duration-overlay:\s*240ms/);
  });

  it("removes alPop and enters from ≥ scale(0.95)", () => {
    expect(cssSrc).not.toMatch(/@keyframes alPop/);
    expect(cssSrc).toMatch(/@keyframes alEnter/);
    expect(cssSrc).toMatch(/from\s*\{\s*transform:\s*scale\(0\.95\)/);
    // No keyframe may enter from scale(0) — comment mentions are fine.
    expect(cssSrc).not.toMatch(/from\s*\{[^}]*scale\(0\)/);
  });

  it("applies reduced-motion globally: sheets/dropdowns lose spatial translate", () => {
    const start = cssSrc.indexOf("@media (prefers-reduced-motion: reduce)");
    expect(start, "prefers-reduced-motion block must exist").toBeGreaterThanOrEqual(0);
    const body = cssSrc.slice(start);
    // Docs sheet and popover must not keep a translate-based keyframe under reduce.
    expect(body).toMatch(/\.gb-sheet/);
    expect(body).toMatch(/\.gb-popover/);
    expect(body).toMatch(/animation-name:\s*gbOverlayIn/);
    expect(body).toMatch(/\.gb-agent-rail\[data-open="false"\][\s\S]*transform:\s*none/);
  });

  it("sabotage: deleting the reduced-motion block fails the reduced-motion contract", () => {
    // The contract above is the live assertion. This documents the sabotage recipe:
    // strip `@media (prefers-reduced-motion: reduce) { … }` from index.css and the
    // previous test fails. Guard that the marker the sabotage removes is present.
    expect(cssSrc).toMatch(/@media \(prefers-reduced-motion:\s*reduce\)/);
  });

  it("Agent rail: pointer uses --duration-overlay; keyboard path is data-motion=instant (0ms)", () => {
    expect(cssSrc).toMatch(
      /\.gb-agent-rail\s*\{[\s\S]*?transition:[\s\S]*?var\(--duration-overlay\)/,
    );
    expect(cssSrc).toMatch(
      /\.gb-agent-rail\[data-motion="instant"\]\s*\{[\s\S]*?transition-duration:\s*0ms/,
    );
    expect(agentSidebarSrc).toMatch(
      /data-motion=\{modality === "keyboard" \? "instant" : "pointer"\}/,
    );
    // AppFrame must sticky the modality at toggle time (not a later pointer event).
    expect(appFrameSrc).toMatch(/setAgentMotion\(liveModality\)/);
    expect(appFrameSrc).toMatch(/modality=\{agentMotion\}/);
  });

  it("sabotage: a 240ms keyboard Agent path would fail", () => {
    // If someone wires keyboard to the pointer duration, data-motion=instant disappears.
    expect(agentSidebarSrc).not.toMatch(/data-motion=\{"pointer"\}/);
    expect(agentSidebarSrc).toMatch(/modality === "keyboard"/);
    const instant = cssSrc.match(/\.gb-agent-rail\[data-motion="instant"\]\s*\{([^}]+)\}/);
    expect(instant?.[1]).toMatch(/transition-duration:\s*0ms/);
    expect(instant?.[1]).not.toMatch(/240ms/);
  });

  it("Dialog drops no-op animate-in and uses gb-overlay / gb-dialog-content", () => {
    expect(dialogSrc).not.toMatch(/animate-in/);
    expect(dialogSrc).toMatch(/gb-overlay/);
    expect(dialogSrc).toMatch(/gb-dialog-content/);
  });

  it("Docs sheet enter/exit share gb-sheet; keyboard is instant", () => {
    expect(docsSrc).toMatch(/gb-sheet/);
    expect(docsSrc).toMatch(/data-motion=\{motion === "keyboard" \? "instant" : "pointer"\}/);
    expect(docsSrc).not.toMatch(/alSlideLeft/);
  });

  it("dropdown uses gb-popover (opacity/scale), not a translate fade", () => {
    expect(dropdownSrc).toMatch(/gb-popover/);
    expect(dropdownSrc).not.toMatch(/animate-fade/);
  });

  it("owned surfaces do not use transition: all", () => {
    const blob = [cssSrc, dialogSrc, dropdownSrc, agentSidebarSrc, appFrameSrc, docsSrc].join("\n");
    expect(blob).not.toMatch(/transition:\s*all/);
  });
});
