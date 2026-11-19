/**
 * GRPH-1728: the rules map strip. A stage card's content fits its card with the longest real
 * tally, the selected card's glow has room inside its foreignObject, and the map draws the code
 * graph's dot grid behind the stages, docked and undocked. jsdom does no layout, so the fit is
 * held by the card's fixed geometry; `e2e/rules-found-bug.play.ts` measures it in a browser.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  LEVEL_ORDER,
  STAGE_CHROME_H,
  STAGE_GLOW,
  STAGE_H,
  STAGE_W,
  STAGE_Y,
  TALLY_ROW_H,
  tally,
  tallyRows,
} from "@/features/rules/levels";
import { RulesView } from "@/features/rules/RulesView";
import { api } from "@/lib/api";
import type { RuleFlowRule, RulesFlow } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  api: {
    rulesFlow: vi.fn(), gitops: vi.fn(), config: vi.fn(), syncStatus: vi.fn(), events: vi.fn(),
    ruleToggles: vi.fn(), setRuleToggles: vi.fn(), ruleEffect: vi.fn(), me: vi.fn(), members: vi.fn(),
    pathCounts: vi.fn(), exclusiveAreasPreview: vi.fn(), unmappedRefusals: vi.fn(),
    recommendedRules: vi.fn(), dismissedRecommendedRules: vi.fn(), refusalCoverage: vi.fn(),
    updateGitops: vi.fn(), fleetPolicy: vi.fn(), ruleProfiles: vi.fn(), ruleProfileDrift: vi.fn(), ruleProfileUpdates: vi.fn(),
  },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core", tag: "CORE", name: "Core" }, projects: [], setActiveId: () => {} }),
}));

const ALL = ["push_to_base", "prs_to_base", "prs_to_integration"];
function rules(stage: string, n: number, enforcement: string, over: Partial<RuleFlowRule> = {}): RuleFlowRule[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `${stage}.${enforcement}_${over.used === false ? "off" : over.unmeasured_reason ? "unm" : "on"}_${i}`, stage,
    title: `${enforcement} ${i}`, summary: "", enforcement, channel: null,
    enforcer: { kind: "graphban_source", ref: "a.py", url: "" }, truth: "db", scope: "project", models: ALL,
    value: true, source: "platform default", used: true, label: null, skills: null, control: "toggle",
    params: { value: {}, source: "platform default" }, locked: false, ...over,
  }) as RuleFlowRule);
}
const UNMEASURED = { unmeasured_reason: "refused by the pack loader" } as Partial<RuleFlowRule>;

// Build on p74-walk: 9 enforced · 3 checked · 1 advisory · 1 unmeasured — the longest real tally.
const build = [...rules("build", 9, "enforced"), ...rules("build", 3, "checked"), ...rules("build", 1, "advisory"), ...rules("build", 1, "checked", UNMEASURED)];
// And a stage at every level there is, two-digit counts in each.
const every = [
  ...rules("review", 12, "enforced"), ...rules("review", 10, "checked"), ...rules("review", 10, "external"),
  ...rules("review", 11, "advisory"), ...rules("review", 13, "checked", UNMEASURED), ...rules("review", 14, "checked", { used: false }),
];

const flow = {
  version: "1.0.0", model: "prs_to_base",
  control: { state: "local", writable: true, message: "", org_name: "" },
  edges: [["build", "review"]],
  stages: [
    { id: "build", title: "Build", summary: "", state: "checked", rules: build },
    { id: "review", title: "Review", summary: "", state: "advisory", rules: every },
  ],
} as unknown as RulesFlow;

function view(url: string) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[url]}>
        <RulesView />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  window.localStorage.clear();
  vi.mocked(api.rulesFlow).mockResolvedValue(flow);
  vi.mocked(api.me).mockResolvedValue({ id: "u1" } as never);
  vi.mocked(api.members).mockResolvedValue([{ user: { id: "u1" }, role: "owner" }] as never);
  vi.mocked(api.pathCounts).mockResolvedValue({ indexed: true, paths: [] });
  vi.mocked(api.exclusiveAreasPreview).mockResolvedValue({ serialised: [] });
  vi.mocked(api.config).mockResolvedValue({ hosted_mode: false } as never);
  vi.mocked(api.syncStatus).mockResolvedValue({ linked: false, org: "" } as never);
  vi.mocked(api.events).mockResolvedValue({ results: [], total: 0 } as never);
  vi.mocked(api.unmappedRefusals).mockResolvedValue({ days: 7, recording_since: null, groups: [] });
  vi.mocked(api.dismissedRecommendedRules).mockResolvedValue([]);
  vi.mocked(api.refusalCoverage).mockResolvedValue({ published: false, n: null, m: null, commit: null, published_at: null });
  vi.mocked(api.ruleToggles).mockResolvedValue({ library_loaded: true, toggles: [] });
  vi.mocked(api.ruleEffect).mockRejectedValue(new Error("no effect"));
  vi.mocked(api.gitops).mockResolvedValue({
    control: { state: "local", writable: true, message: "" },
    fields: { base_branch: { value: "main" }, no_push_to_base: { value: true }, branch_name_pattern: { value: null }, pr_title_pattern: { value: null }, reviewer_bar: { value: "sign_off", source: "project" } },
    version_from: { value: null }, release_defined_in: { value: null }, model: { value: "x" },
  } as never);
});

afterEach(() => vi.restoreAllMocks());

describe("rules map strip (GRPH-1728)", () => {
  it("every stage card's content fits its card, with the longest real tally and with every level", async () => {
    view("/rules?stage=build");
    for (const [id, rs] of [["build", build], ["review", every]] as const) {
      const card = await screen.findByTestId(`rules-stage-${id}`);
      const line = within(card).getByTestId("rules-stage-tally");
      // One level to a cell, two cells to a row, no cell wraps: the rows are known before layout.
      expect(line.style.gridTemplateColumns).toBe("repeat(2, minmax(0, 1fr))");
      expect(line.style.lineHeight).toBe(`${TALLY_ROW_H}px`);
      for (const cell of Array.from(line.children)) expect(cell.className).toContain("whitespace-nowrap");
      expect(line.children).toHaveLength(tally(rs).length);
      expect(Number(line.dataset.rows)).toBe(tallyRows(rs));
      // ...and the card is tall enough for them.
      expect(STAGE_CHROME_H + tallyRows(rs) * TALLY_ROW_H).toBeLessThanOrEqual(STAGE_H);
    }
    expect(tallyRows(build)).toBe(2);
    expect(tallyRows(every)).toBe(Math.ceil(LEVEL_ORDER.length / 2));
  });

  it("the selected card's glow has room inside its foreignObject", async () => {
    view("/rules?stage=build");
    const card = await screen.findByTestId("rules-stage-build");
    expect(card).toHaveAttribute("data-selected", "true");
    // Both shadows start at the card's edge: the 3px ring and the 24px blur, so the glow reaches
    // 24px out; the hover lifts the card another 2px.
    expect(STAGE_GLOW).toBeGreaterThanOrEqual(Math.max(3, 24) + 2);
    const box = screen.getByTestId("rules-stage-box-build");
    expect(Number(box.getAttribute("y"))).toBe(STAGE_Y - STAGE_GLOW);
    expect(Number(box.getAttribute("width"))).toBe(STAGE_W + 2 * STAGE_GLOW);
    expect(Number(box.getAttribute("height"))).toBe(STAGE_H + 2 * STAGE_GLOW);
    expect((card.parentElement as HTMLElement).style.padding).toBe(`${STAGE_GLOW}px`);
  });

  it("the rules map draws the code graph's dot grid behind the stages, docked and undocked", async () => {
    for (const url of ["/rules", "/rules?stage=build"]) {
      const { unmount } = view(url);
      await screen.findByTestId("rules-stage-build");
      expect(screen.getByTestId("rules-canvas").dataset.docked).toBe(url.includes("stage") ? "true" : undefined);
      const dots = screen.getByTestId("rules-dots");
      expect(dots.getAttribute("fill")).toBe("url(#rules-dots)");
      const pattern = document.getElementById("rules-dots")!;
      expect(pattern.tagName.toLowerCase()).toBe("pattern");
      expect(pattern.getAttribute("width")).toBe("24");
      expect(pattern.getAttribute("patternUnits")).toBe("userSpaceOnUse");
      const dot = pattern.querySelector("circle")!;
      expect([dot.getAttribute("r"), dot.getAttribute("fill")]).toEqual(["1", "#1a2026"]);
      // Behind the stages: the grid is the first thing the map draws.
      const stage = screen.getByTestId("rules-stage-box-build");
      expect(dots.compareDocumentPosition(stage) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(dots.parentElement!.firstElementChild).toBe(dots);
      unmount();
    }
  });
});
