/** GRPH-1405: the test-scope rule shows its three modes as one choice and writes the pick. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RulesView } from "@/features/rules/RulesView";
import { api } from "@/lib/api";
import type { GitopsView, RuleFlowRule, RulesFlow, RuleToggles } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  api: {
    rulesFlow: vi.fn(), gitops: vi.fn(), config: vi.fn(), syncStatus: vi.fn(), events: vi.fn(),
    ruleToggles: vi.fn(), setRuleToggle: vi.fn(),
  },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core", tag: "CORE", name: "Core" }, projects: [], setActiveId: () => {} }),
}));

const RULE = "build.test_scope";

function rule(mode: string): RuleFlowRule {
  return {
    id: RULE, stage: "build", title: "Where the test suites run", summary: "s", enforcement: "checked", channel: null,
    enforcer: { kind: "doc", ref: "docs/rules.md", url: "" }, truth: "db", scope: "project",
    models: ["push_to_base", "prs_to_base", "prs_to_integration"], control: "toggle",
    value: true, source: "platform default", params: { value: { mode }, source: "project" },
    locked: false, used: true, label: null, skills: null,
  } as RuleFlowRule;
}

async function open(mode: string) {
  const flow: RulesFlow = {
    version: "1.0.0", model: "prs_to_base",
    control: { state: "local", writable: true, message: "", org_name: "" }, edges: [],
    stages: [{ id: "build", title: "Build", summary: "b", state: "checked", rules: [rule(mode)] }],
  } as RulesFlow;
  vi.mocked(api.rulesFlow).mockResolvedValue(flow);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/rules?stage=build"]}>
        <RulesView />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return screen.findByTestId("rules-inspector");
}

beforeEach(() => {
  vi.resetAllMocks();
  const f = <T,>(value: T) => ({ value, source: "project" as const });
  vi.mocked(api.gitops).mockResolvedValue({
    project_id: "core", org_id: null,
    fields: { base_branch: f("main"), no_push_to_base: f(null), branch_name_pattern: f(null), pr_title_pattern: f(null), reviewer_bar: f(null) },
    control: { state: "local", writable: true, message: "" }, was: null,
    version_from: f(null), release_defined_in: f(null), model: f(null), plan: null, projects: [],
  } as unknown as GitopsView);
  vi.mocked(api.config).mockResolvedValue({ hosted_mode: false } as never);
  vi.mocked(api.syncStatus).mockResolvedValue({ linked: false, org: "" } as never);
  vi.mocked(api.ruleToggles).mockResolvedValue({ library_loaded: true, toggles: [] } satisfies RuleToggles);
  vi.mocked(api.events).mockResolvedValue({ results: [], total: 0 } as never);
});

describe("test-scope rule", () => {
  it("shows three modes with a line each and the stored one selected", async () => {
    const ins = await open("agent_targeted");
    const group = within(ins).getByRole("radiogroup", { name: "Where the test suites run" });
    expect(within(group).getAllByRole("radio")).toHaveLength(3);
    expect(within(group).getByRole("radio", { name: "The agent runs partial CI, only what changed" })).toBeChecked();
    expect(within(group).getByRole("radio", { name: "The agent runs CI" })).not.toBeChecked();
    expect(group).toHaveTextContent("the PR's CI runs the full suites");
  });

  it("choosing a mode writes it with a reason", async () => {
    const user = userEvent.setup();
    vi.mocked(api.setRuleToggle).mockResolvedValue({} as never);
    const ins = await open("agent_and_pr");
    await user.click(within(ins).getByRole("radio", { name: "The agent runs CI" }));
    await user.click(within(ins).getByRole("button", { name: "Change params" }));
    await user.type(within(ins).getByLabelText("Reason"), "solo project");
    await user.click(within(ins).getByRole("button", { name: "Change params" }));
    await waitFor(() =>
      expect(api.setRuleToggle).toHaveBeenCalledWith("core", RULE, { params: { mode: "agent_full" }, reason: "solo project" }),
    );
  });
});
