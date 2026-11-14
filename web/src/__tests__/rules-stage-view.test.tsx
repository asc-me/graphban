/**
 * The full-width stage view (GRPH-1659): the paths the read view hands to an editor — a
 * refusal's "set this value" prefill, the per-row Edit for rules the stage editor can't edit,
 * the pack opening the same view, Escape on the view — plus the width classes and the path
 * count's "counted as" label.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RulesView } from "@/features/rules/RulesView";
import { RuleValueEditor } from "@/features/rules/RuleValueEditor";
import { api } from "@/lib/api";
import type { GitopsView, RuleFlowRule, RulesFlow } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  api: {
    rulesFlow: vi.fn(), gitops: vi.fn(), config: vi.fn(), syncStatus: vi.fn(), events: vi.fn(),
    ruleToggles: vi.fn(), me: vi.fn(), members: vi.fn(), fleetPolicy: vi.fn(), pathCounts: vi.fn(),
    unmappedRefusals: vi.fn(), recommendedRules: vi.fn(), dismissedRecommendedRules: vi.fn(),
    refusalCoverage: vi.fn(),
  },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core", tag: "CORE", name: "Core" }, projects: [], setActiveId: () => {} }),
}));

const ALL = ["push_to_base", "prs_to_base", "prs_to_integration"];
const ctx = { kind: "context" as const, ref: "get_context.gitops", url: "" };

function rule(over: Partial<RuleFlowRule> & Pick<RuleFlowRule, "id" | "stage">): RuleFlowRule {
  return {
    title: over.id, summary: `${over.id} summary`, enforcement: "advisory", channel: null,
    enforcer: { kind: "graphban_source", ref: "a.py", url: "" }, truth: "code", scope: "project", models: ALL,
    value: null, source: null, used: true, label: null, skills: null, ...over,
  } as RuleFlowRule;
}

const flow: RulesFlow = {
  version: "1.0.0", model: "prs_to_base",
  control: { state: "local", writable: true, message: "", org_name: "" },
  edges: [["plan", "build"], ["build", "release"], ["release", "learn"]],
  stages: [
    { id: "plan", title: "Plan", summary: "Plan summary", state: "external", rules: [
      rule({ id: "plan.budget_cap", stage: "plan", enforcement: "external", title: "Budget cap", truth: "db", value: "5000", source: "project",
        enforcer: { kind: "supervisor", ref: "gbfleet budget cap", url: "" } }),
    ] },
    { id: "build", title: "Build", summary: "Build summary", state: "advisory", rules: [
      rule({ id: "agents.skills_pack", stage: "build", channel: "skill", truth: "repo_file", title: "Agent instructions (Graphban pack)",
        enforcer: { kind: "skill", ref: "gban skills", url: "" }, skills_version: "0.8.2",
        skills: [{ name: "graphban-review", url: "https://example.test/review" }] }),
      rule({ id: "agents.operating_loop", stage: "build", channel: "doc", truth: "repo_file", enforcer: { kind: "doc", ref: "AGENTS.md", url: "" } }),
    ] },
    { id: "release", title: "Release", summary: "Release summary", state: "advisory", rules: [
      rule({ id: "release.release_defined_in", stage: "release", channel: "context", truth: "db", enforcer: ctx, title: "Release doc", source: "unmeasured" }),
    ] },
    { id: "learn", title: "Learn", summary: "Learn summary", state: "advisory", rules: [
      rule({ id: "memory.write_mode", stage: "learn", control: "select", truth: "db", value: "review", title: "Memory write mode" }),
    ] },
  ],
};

function gitops(): GitopsView {
  const f = <T,>(value: T) => ({ value, source: "project" as const });
  return {
    project_id: "core", org_id: null,
    fields: { base_branch: f("main"), no_push_to_base: f(null), branch_name_pattern: f(null), pr_title_pattern: f(null), reviewer_bar: f(null) },
    control: { state: "local", writable: true, message: "" }, was: null,
    version_from: f(null), release_defined_in: f(null), model: f(null), plan: null, projects: [],
  };
}

const ME = { id: "u1", name: "Alex", handle: "alex", email: "", avatar: "", initials: "A" };

function wrap(ui: React.ReactElement, url = "/rules") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.rulesFlow).mockResolvedValue(flow);
  vi.mocked(api.gitops).mockResolvedValue(gitops());
  vi.mocked(api.config).mockResolvedValue({ hosted_mode: false } as never);
  vi.mocked(api.syncStatus).mockResolvedValue({ linked: false, org: "" } as never);
  vi.mocked(api.ruleToggles).mockResolvedValue({ library_loaded: true, toggles: [] });
  vi.mocked(api.events).mockResolvedValue({ results: [], total: 0 } as never);
  vi.mocked(api.me).mockResolvedValue(ME as never);
  vi.mocked(api.members).mockResolvedValue([{ user: ME, role: "owner", access: "write" }] as never);
  vi.mocked(api.fleetPolicy).mockResolvedValue({ policy: { caps: { per_item_tokens: 5000 } }, writable: true } as never);
  vi.mocked(api.unmappedRefusals).mockResolvedValue({ days: 7, recording_since: null, groups: [] });
  vi.mocked(api.dismissedRecommendedRules).mockResolvedValue([]);
  vi.mocked(api.refusalCoverage).mockResolvedValue({ published: false, n: null, m: null, commit: null, published_at: null });
});

describe("full-width stage view (GRPH-1659)", () => {
  it("a 'set this value' suggestion opens the stage editor with the value staged on its rule", async () => {
    const user = userEvent.setup();
    vi.mocked(api.recommendedRules).mockResolvedValue({
      sources: [],
      suggestions: [{ id: "a", stage: "release", level: "advisory", title: "Name the release doc", why: "w", kind: "value",
        rule_id: "release.release_defined_in", value: "RELEASING.md", evidence: [], fingerprint: "f" }],
    } as never);
    wrap(<RulesView />);
    await user.click(await screen.findByTestId("rules-open-recommended"));
    await user.click(await screen.findByRole("button", { name: "Open in Release" }));
    const ins = await screen.findByRole("region", { name: "Release stage inspector" });
    const editor = await within(ins).findByTestId("rules-stage-editor");
    const field = within(editor).getByTestId("rules-edit-gitops-release_defined_in");
    expect(await within(field).findByLabelText("Release doc")).toHaveValue("RELEASING.md");
    expect(within(ins).queryByTestId("rules-edit-stage")).toBeNull();
  });

  it("memory.write_mode's per-row Edit opens its editor in the view; Done closes it", async () => {
    const user = userEvent.setup();
    wrap(<RulesView />, "/rules?stage=learn");
    const ins = await screen.findByTestId("rules-inspector");
    const row = within(ins).getByTestId("rules-row-memory.write_mode");
    expect(within(row).getByTestId("rules-value")).toHaveTextContent("Review");
    expect(within(row).queryByTestId("rules-write-mode")).toBeNull();
    await user.click(await within(row).findByTestId("rules-edit-row-memory.write_mode"));
    expect(within(row).getByTestId("rules-write-mode")).toBeInTheDocument();
    expect(within(row).getByRole("radiogroup", { name: "Memory write mode" })).toBeInTheDocument();
    expect(within(row).queryByTestId("rules-value")).toBeNull();
    await user.click(within(row).getByRole("button", { name: "Done" }));
    expect(within(row).queryByTestId("rules-write-mode")).toBeNull();
    expect(within(row).getByTestId("rules-value")).toHaveTextContent("Review");
  });

  it("plan.budget_cap's per-row Edit opens the budget editor in the view; Done closes it", async () => {
    const user = userEvent.setup();
    wrap(<RulesView />, "/rules?stage=plan");
    const ins = await screen.findByTestId("rules-inspector");
    const row = within(ins).getByTestId("rules-row-plan.budget_cap");
    expect(within(row).queryByTestId("rules-budget-cap")).toBeNull();
    await user.click(await within(row).findByTestId("rules-edit-row-plan.budget_cap"));
    expect(await within(row).findByTestId("rules-budget-cap")).toHaveTextContent("5000 tokens per item");
    await user.click(within(row).getByRole("button", { name: "Done" }));
    expect(within(row).queryByTestId("rules-budget-cap")).toBeNull();
  });

  it("the pack opens the same full-width view, with the strip, from the map and from the strip", async () => {
    const user = userEvent.setup();
    wrap(<RulesView />);
    await user.click(await screen.findByTestId("rules-pack"));
    const ins = await screen.findByRole("region", { name: "Agent instructions (Graphban pack) inspector" });
    expect(ins).toHaveClass("inset-0");
    const strip = within(ins).getByTestId("rules-stage-strip");
    expect(within(strip).getByTestId("rules-strip-pack")).toHaveAttribute("aria-current", "true");
    expect(within(ins).getByTestId("rules-pack-skills")).toHaveTextContent("skills/graphban-review/SKILL.md");
    // From a stage, the strip's pack entry opens the pack in the same view.
    await user.click(within(strip).getByTestId("rules-strip-s:build"));
    await screen.findByRole("region", { name: "Build stage inspector" });
    await user.click(screen.getByTestId("rules-strip-pack"));
    expect(await screen.findByRole("region", { name: "Agent instructions (Graphban pack) inspector" })).toBeInTheDocument();
  });

  it("Escape on the stage view closes it and returns to the map", async () => {
    const user = userEvent.setup();
    wrap(<RulesView />, "/rules?stage=build");
    const ins = await screen.findByRole("region", { name: "Build stage inspector" });
    await waitFor(() => expect(ins.contains(document.activeElement)).toBe(true));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("rules-inspector")).not.toBeInTheDocument());
    expect(screen.getByTestId("rules-listbox")).toBeInTheDocument();
  });

  it("width: the content is capped at 880px, the strip scrolls horizontally, and the view has no horizontal overflow", async () => {
    wrap(<RulesView />, "/rules?stage=build");
    const ins = await screen.findByTestId("rules-inspector");
    expect(ins).toHaveClass("inset-0", "overflow-hidden");
    const strip = within(ins).getByTestId("rules-stage-strip");
    expect(strip).toHaveClass("overflow-x-auto");
    for (const b of within(strip).getAllByRole("button")) expect(b).toHaveClass("flex-none", "whitespace-nowrap");
    expect(ins.querySelector(".max-w-\\[880px\\]")).not.toBeNull();
    // The old narrow column and bottom sheet are gone.
    expect(ins).not.toHaveClass("w-[400px]");
    expect(ins).not.toHaveClass("bottom-0");
  });
});

describe("PathCountBadge (GRPH-1659)", () => {
  function renderPaths(paths: string[]) {
    return wrap(
      <RuleValueEditor ruleId="build.generated_files" name="paths" value={paths} onChange={() => {}} onError={() => {}} />,
    );
  }

  it("says 'counted as <normalised>' only when the count normalised the path, never 'saved as'", async () => {
    vi.mocked(api.pathCounts).mockImplementation(async (_p: string, paths: string[]) => ({
      indexed: true,
      paths: paths.map((p) => ({ path: p, normalised: p.replace(/\/$/, ""), files: 4 })),
    }));
    renderPaths(["client/generated/", "api/gen"]);
    await waitFor(() => expect(screen.getAllByTestId("rules-path-count").map((b) => b.textContent)).toEqual([
      "counted as client/generated · 4 files",
      "4 files",
    ]));
    expect(document.body.textContent).not.toMatch(/saved as/);
  });

  it("a 40-character path is the input's value in full, and the input grows to the row (flex-1, no fixed width)", async () => {
    const long = "services/payments/generated/clients/v20/";
    expect(long).toHaveLength(40);
    vi.mocked(api.pathCounts).mockResolvedValue({ indexed: true, paths: [{ path: long, normalised: long, files: 1 }] });
    renderPaths([long]);
    const input = await screen.findByDisplayValue(long);
    expect(input).toHaveClass("flex-1");
    expect(input.className).not.toMatch(/\bw-\[\d+px\]/);
  });
});
