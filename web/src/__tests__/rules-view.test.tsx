import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { legendCounts, stageLevel } from "@/features/rules/levels";
import { RulesView } from "@/features/rules/RulesView";
import { SECTIONS } from "@/components/shell/LeftNav";
import { api } from "@/lib/api";
import type { RuleFlowRule, RuleStage, RulesFlow } from "@/lib/types";

vi.mock("@/lib/api", () => ({ api: { rulesFlow: vi.fn() } }));

vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({
    activeId: "core",
    active: { id: "core", tag: "CORE", name: "Core" },
    projects: [],
    setActiveId: () => {},
  }),
}));

const ALL = ["push_to_base", "prs_to_base", "prs_to_integration"];

function rule(over: Partial<RuleFlowRule> & Pick<RuleFlowRule, "id" | "stage" | "enforcement">): RuleFlowRule {
  return {
    title: over.id,
    summary: `${over.id} summary`,
    channel: null,
    enforcer: { kind: "graphban_source", ref: "app/services/fleet.py", url: "https://github.com/asc-me/graphban/blob/v1.0.0/backend/app/services/fleet.py" },
    truth: "code",
    scope: "project",
    models: ALL,
    value: null,
    source: null,
    used: true,
    label: null,
    // The response model sends this on every rule, not only the pack.
    skills: null,
    ...over,
  };
}

const ctx = { kind: "context", ref: "get_context.gitops", url: "" };

function stage(id: string, title: string, state: RuleStage["state"], rules: RuleFlowRule[]): RuleStage {
  return { id, title, summary: `${title} summary`, state, rules };
}

/** Mirrors `rules.flow()` for a prs_to_base project. Review: 2 enforced + 2 advisory. */
function flowFixture(over: Partial<RulesFlow> = {}): RulesFlow {
  return {
    version: "1.0.0",
    model: "prs_to_base",
    edges: [
      ["spec", "plan"], ["plan", "build"], ["build", "review"],
      ["review", "merge"], ["merge", "release"], ["release", "learn"],
    ],
    stages: [
      stage("spec", "Spec", "enforced", [
        rule({ id: "gate.approval_earned_by_grill", stage: "spec", enforcement: "enforced", title: "Approval is earned by the grill" }),
      ]),
      stage("plan", "Plan", "enforced", [
        rule({ id: "gate.decompose_requires_approval", stage: "plan", enforcement: "enforced" }),
      ]),
      stage("build", "Build", "external", [
        rule({ id: "budget.cap", stage: "build", enforcement: "external", title: "Budget cap", scope: "org",
          enforcer: { kind: "supervisor", ref: "gbfleet budget cap", url: "" } }),
        rule({ id: "agents.operating_loop", stage: "build", enforcement: "advisory", channel: "doc", truth: "repo_file",
          enforcer: { kind: "doc", ref: "AGENTS.md", url: "" } }),
        rule({ id: "agents.skills_pack", stage: "build", enforcement: "advisory", channel: "skill", truth: "repo_file",
          title: "Agent instructions (Graphban pack)", summary: "Skills the agents read.",
          enforcer: { kind: "skill", ref: "gban skills", url: "" }, skills: ["graphban-review", "graphban-work"] }),
      ]),
      stage("review", "Review", "advisory", [
        rule({ id: "review.not_own_work", stage: "review", enforcement: "enforced", title: "Not your own work" }),
        rule({ id: "review.role_gated_tools", stage: "review", enforcement: "enforced", title: "Role-gated tools" }),
        rule({ id: "review.reviewer_diversity", stage: "review", enforcement: "advisory", channel: "doc", title: "Reviewer diversity" }),
        rule({ id: "gitops.reviewer_bar", stage: "review", enforcement: "advisory", channel: "context", truth: "db",
          enforcer: ctx, value: "graphban", source: "project", title: "Reviewer bar" }),
      ]),
      stage("merge", "Merge", "unmeasured", [
        rule({ id: "gitops.base_branch", stage: "merge", enforcement: "advisory", channel: "context", truth: "db",
          enforcer: ctx, value: "main", source: "project", title: "Base branch" }),
        rule({ id: "gitops.pr_title_pattern", stage: "merge", enforcement: "unmeasured", truth: "db",
          enforcer: ctx, source: "unmeasured", title: "PR title pattern" }),
        rule({ id: "ci.backend_suite_both_engines", stage: "merge", enforcement: "checked", truth: "repo_file",
          enforcer: { kind: "ci_job", ref: ".github/workflows/ci.yml", url: "" }, title: "Backend suite on both engines" }),
        rule({ id: "forge.branch_protection", stage: "merge", enforcement: "external",
          enforcer: { kind: "forge", ref: "branch protection", url: "" }, title: "Forge branch protection" }),
      ]),
      stage("release", "Release", "not_used", [
        rule({ id: "release.version_from", stage: "release", enforcement: "advisory", channel: "context", truth: "db",
          enforcer: ctx, value: "tag", source: "project", used: false, label: "not used by this project's model" }),
      ]),
      stage("learn", "Learn", "unmeasured", []),
    ],
    ...over,
  };
}

function renderAt(path = "/rules") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <RulesView />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const pill = (stageId: string) => within(screen.getByTestId(`rules-stage-${stageId}`)).getByTestId("rules-stage-pill");

describe("RulesView", () => {
  beforeEach(() => {
    vi.mocked(api.rulesFlow).mockReset();
    vi.mocked(api.rulesFlow).mockResolvedValue(flowFixture());
  });

  it("draws every stage from the route, in order, with its fixed edges", async () => {
    renderAt();
    await screen.findByTestId("rules-canvas");
    expect(api.rulesFlow).toHaveBeenCalledWith("core");
    const ids = screen.getAllByRole("option").map((o) => o.getAttribute("data-testid"));
    expect(ids).toEqual([
      "rules-stage-spec", "rules-stage-plan", "rules-stage-build", "rules-stage-review",
      "rules-stage-merge", "rules-stage-release", "rules-stage-learn", "rules-pack",
    ]);
    expect(screen.getAllByTestId("rules-edge")).toHaveLength(6);
    expect(screen.getByTestId("rules-model")).toHaveTextContent("prs_to_base");
  });

  it("renders nothing it was not sent: a different payload draws a different map", async () => {
    vi.mocked(api.rulesFlow).mockResolvedValue(
      flowFixture({ stages: [stage("spec", "Spec", "advisory", [rule({ id: "x.only", stage: "spec", enforcement: "advisory" })])], edges: [] }),
    );
    renderAt();
    await screen.findByTestId("rules-canvas");
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(screen.queryByTestId("rules-edge")).not.toBeInTheDocument();
    expect(screen.getByTestId("rules-legend-advisory")).toHaveTextContent("1");
    expect(screen.getByTestId("rules-legend-enforced")).toHaveTextContent("0");
  });

  it("Review with 2 enforced + 2 advisory is drawn ADVISORY, with icon and word", async () => {
    renderAt();
    await screen.findByTestId("rules-canvas");
    const p = pill("review");
    expect(p).toHaveTextContent("ADVISORY");
    expect(p.querySelector("svg[data-level='advisory']")).not.toBeNull();
    expect(screen.getByTestId("rules-stage-review")).toHaveTextContent("2 enforced");
    expect(screen.getByTestId("rules-stage-review")).toHaveTextContent("2 advisory");
  });

  it("never draws a stage stronger than its weakest used rule, even if the payload says so", () => {
    const s = stage("review", "Review", "enforced", [
      rule({ id: "a", stage: "review", enforcement: "enforced" }),
      rule({ id: "b", stage: "review", enforcement: "advisory" }),
      rule({ id: "c", stage: "review", enforcement: "unmeasured", used: false }),
    ]);
    expect(stageLevel(s)).toBe("advisory");
    expect(stageLevel(stage("learn", "Learn", "unmeasured", []))).toBeNull();
    expect(stageLevel(stage("release", "Release", "not_used", [rule({ id: "d", stage: "release", enforcement: "advisory", used: false })]))).toBe("not_used");
  });

  it("legend shows every level with icon + word, counting each registry rule once (not the pack)", async () => {
    renderAt();
    await screen.findByTestId("rules-canvas");
    const expected = legendCounts(flowFixture());
    expect(expected).toEqual({ enforced: 4, checked: 1, external: 2, advisory: 4, unmeasured: 1, not_used: 1 });
    for (const [level, word] of [
      ["enforced", "ENFORCED"], ["checked", "CHECKED"], ["external", "EXTERNAL"],
      ["advisory", "ADVISORY"], ["unmeasured", "UNMEASURED"], ["not_used", "NOT USED"],
    ] as const) {
      const row = screen.getByTestId(`rules-legend-${level}`);
      expect(row).toHaveTextContent(word);
      expect(row).toHaveTextContent(String(expected[level]));
      expect(row.querySelector("svg")).not.toBeNull();
    }
  });

  it("opens the Merge inspector weakest first with the honest per-row states", async () => {
    const user = userEvent.setup();
    renderAt();
    await user.click(await screen.findByTestId("rules-stage-merge"));
    const ins = await screen.findByTestId("rules-inspector");
    const rows = within(ins).getAllByTestId(/^rules-row-/).map((r) => r.getAttribute("data-testid"));
    expect(rows).toEqual([
      "rules-row-gitops.pr_title_pattern",
      "rules-row-gitops.base_branch",
      "rules-row-forge.branch_protection",
      "rules-row-ci.backend_suite_both_engines",
    ]);
    const pr = within(ins).getByTestId("rules-row-gitops.pr_title_pattern");
    expect(pr).toHaveTextContent("UNMEASURED");
    expect(pr).toHaveTextContent("— no value");
    const base = within(ins).getByTestId("rules-row-gitops.base_branch");
    expect(base).toHaveTextContent("Told to agents in every get_context. Nothing refuses a violation.");
    expect(base).toHaveTextContent("PROJECT");
    expect(base).toHaveTextContent("main");
    expect(within(base).getByTestId("rules-enforcer")).toHaveTextContent("CONTEXT");
    // No URL from the server is no link.
    expect(within(base).getByTestId("rules-enforcer").tagName).toBe("SPAN");
    const forge = within(ins).getByTestId("rules-row-forge.branch_protection");
    expect(forge).toHaveTextContent("NOT VERIFIED");
    const ci = within(ins).getByTestId("rules-row-ci.backend_suite_both_engines");
    expect(within(ci).getByTestId("rules-no-result")).toHaveTextContent("NO RESULT RECORDED");
    expect(ci).toHaveTextContent("That is not a pass.");
    // Read-only map: no inputs anywhere (editing is UI 2/3).
    expect(within(ins).queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("links a source enforcer with its kind prefix and says code rules are held in code", async () => {
    const user = userEvent.setup();
    renderAt();
    await user.click(await screen.findByTestId("rules-stage-review"));
    const row = within(await screen.findByTestId("rules-inspector")).getByTestId("rules-row-review.not_own_work");
    const link = within(row).getByRole("link");
    expect(link).toHaveTextContent("SOURCE");
    expect(link).toHaveTextContent("graphban · app/services/fleet.py @ v1.0.0");
    expect(link).toHaveAttribute("href", "https://github.com/asc-me/graphban/blob/v1.0.0/backend/app/services/fleet.py");
    expect(row).toHaveTextContent("Held in code. Change it with a PR, not here.");
  });

  it("an empty stage says nothing is registered, not clean (2d)", async () => {
    const user = userEvent.setup();
    renderAt();
    await screen.findByTestId("rules-canvas");
    expect(pill("learn")).toHaveTextContent("NO RULES");
    await user.click(screen.getByTestId("rules-stage-learn"));
    expect(await screen.findByTestId("rules-stage-empty")).toHaveTextContent(
      "That isn't the same as nothing applying.",
    );
  });

  it("not used is per rule and stays drawn, dimmed and labelled (6a)", async () => {
    const user = userEvent.setup();
    renderAt();
    await screen.findByTestId("rules-canvas");
    expect(pill("release")).toHaveTextContent("NOT USED");
    await user.click(screen.getByTestId("rules-stage-release"));
    const ins = await screen.findByTestId("rules-inspector");
    expect(ins).toHaveTextContent("Every rule on this stage is not used by this project's model: prs_to_base.");
    const row = within(ins).getByTestId("rules-row-release.version_from");
    expect(row).toHaveAttribute("data-level", "not_used");
    expect(within(row).getByTestId("rules-row-not-used")).toBeInTheDocument();
  });

  it("the pack is one collapsed node listing the shipped skills", async () => {
    const user = userEvent.setup();
    renderAt();
    await user.click(await screen.findByTestId("rules-pack"));
    const ins = await screen.findByTestId("rules-inspector");
    expect(ins).toHaveTextContent("2 skills shipped with gban");
    expect(within(ins).getByTestId("rules-pack-skills")).toHaveTextContent("skills/graphban-review/SKILL.md");
    // Build tallies its own rules; the pack is not counted on the card.
    expect(screen.getByTestId("rules-stage-build")).toHaveTextContent("1 external");
    expect(screen.getByTestId("rules-stage-build")).toHaveTextContent("1 advisory");
  });

  it("the canvas is one keyboard listbox: arrows move, Enter opens, Escape closes", async () => {
    const user = userEvent.setup();
    renderAt();
    const lb = await screen.findByTestId("rules-listbox");
    lb.focus();
    await user.keyboard("{ArrowRight}");
    expect(lb.getAttribute("aria-activedescendant")).toBe(screen.getByTestId("rules-stage-spec").id);
    await user.keyboard("{ArrowRight}{ArrowRight}{ArrowRight}");
    expect(lb.getAttribute("aria-activedescendant")).toBe(screen.getByTestId("rules-stage-review").id);
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("dialog", { name: "Review stage inspector" })).toBeInTheDocument();
    expect(screen.getByTestId("rules-stage-review")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("rules-stage-review")).toHaveAttribute(
      "aria-label",
      "Stage Review, drawn as advisory. 2 enforced, 2 advisory",
    );
    lb.focus();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("rules-inspector")).not.toBeInTheDocument());
  });

  it("?stage=merge lands with Merge open (the Settings pointer)", async () => {
    renderAt("/rules?stage=merge");
    expect(await screen.findByRole("dialog", { name: "Merge stage inspector" })).toBeInTheDocument();
  });

  it("a fresh project says the contract is unmeasured, and the banner opens Merge (6b)", async () => {
    const user = userEvent.setup();
    vi.mocked(api.rulesFlow).mockResolvedValue(flowFixture({ model: null }));
    renderAt();
    const banner = await screen.findByTestId("rules-fresh-banner");
    expect(banner).toHaveTextContent("Your delivery contract is unmeasured.");
    expect(screen.getByTestId("rules-model")).toHaveTextContent("unmeasured");
    await user.click(within(banner).getByRole("button", { name: "Open Merge" }));
    expect(await screen.findByRole("dialog", { name: "Merge stage inspector" })).toBeInTheDocument();
  });

  it("loading shows skeletons and a legend without counts (6c)", async () => {
    vi.mocked(api.rulesFlow).mockReturnValue(new Promise(() => {}));
    renderAt();
    expect(await screen.findByTestId("rules-loading")).toBeInTheDocument();
    expect(screen.getByTestId("rules-legend-enforced")).toHaveTextContent(/^ENFORCED$/);
  });

  it("a failed read draws no map and offers Retry (6d)", async () => {
    const user = userEvent.setup();
    vi.mocked(api.rulesFlow).mockRejectedValueOnce(new Error(JSON.stringify({ detail: "Service Unavailable" })));
    renderAt();
    const err = await screen.findByTestId("rules-error");
    expect(err).toHaveTextContent("GET /api/rules/flow → Service Unavailable");
    expect(screen.queryByTestId("rules-canvas")).not.toBeInTheDocument();
    await user.click(within(err).getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("rules-canvas")).toBeInTheDocument();
  });

  describe("narrow (6e)", () => {
    const wide = window.innerWidth;
    beforeEach(() => {
      Object.defineProperty(window, "innerWidth", { configurable: true, value: 820 });
    });
    afterEach(() => {
      Object.defineProperty(window, "innerWidth", { configurable: true, value: wide });
    });

    it("lists the stages vertically with the same pills, inspector as a sheet", async () => {
      const user = userEvent.setup();
      renderAt();
      const list = await screen.findByTestId("rules-narrow");
      expect(within(list).getAllByRole("listitem")).toHaveLength(7);
      expect(within(screen.getByTestId("rules-stage-review")).getByTestId("rules-stage-pill")).toHaveTextContent("ADVISORY");
      await user.click(screen.getByTestId("rules-stage-review"));
      expect(await screen.findByTestId("rules-inspector")).toHaveClass("bottom-0");
    });
  });
});

describe("Rules nav entry", () => {
  it("sits in Build after MCP Tools (design order)", () => {
    const build = SECTIONS.find((s) => s.key === "build")!;
    const views = build.pages.map((p) => p.view);
    expect(views.indexOf("rules")).toBe(views.indexOf("mcp-tools") + 1);
    expect(build.pages.find((p) => p.view === "rules")?.label).toBe("Rules");
  });
});
