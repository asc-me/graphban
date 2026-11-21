/** GRPH-1703: the found-bug rule shows its mode, and that the default is in force when off or absent. */
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
    ruleToggles: vi.fn(), setRuleToggle: vi.fn(), setRuleToggles: vi.fn(),
  },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core", tag: "CORE", name: "Core" }, projects: [], setActiveId: () => {} }),
}));

const RULE = "build.found_bug";

function rule(mode: string, on = true): RuleFlowRule {
  return {
    id: RULE, stage: "build", title: "What a builder does with a bug it finds", summary: "s", enforcement: "checked", channel: null,
    enforcer: { kind: "doc", ref: "docs/rules.md", url: "" }, truth: "db", scope: "project",
    models: ["push_to_base", "prs_to_base", "prs_to_integration"], control: "toggle",
    value: on, source: "platform default", params: { value: { mode }, source: "project" },
    locked: false, used: on, label: null, skills: null,
  } as RuleFlowRule;
}

async function open(mode: string, on = true, absent = false) {
  const flow: RulesFlow = {
    version: "1.0.0", model: "prs_to_base",
    control: { state: "local", writable: true, message: "", org_name: "" }, edges: [],
    stages: [{ id: "build", title: "Build", summary: "b", state: "checked", rules: absent ? [] : [rule(mode, on)] }],
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

describe("found-bug rule", () => {
  it("shows the effective mode when on", async () => {
    const ins = await open("file_ticket");
    const row = within(ins).getByTestId(`rules-row-${RULE}`);
    expect(within(ins).getByRole("radio", { name: "File a ticket" })).toBeChecked();
    expect(within(row).getByTestId("rules-found-bug-effective")).toHaveTextContent("Effective mode: File a ticket");
  });

  it("says the default fix_in_build is in force when the toggle is off", async () => {
    const ins = await open("file_ticket", false);
    const row = within(ins).getByTestId(`rules-row-${RULE}`);
    expect(within(row).getByTestId("rules-found-bug-effective")).toHaveTextContent("the default, fix_in_build, is in force");
  });

  it("says the default is in force when the pack has no template", async () => {
    const ins = await open("fix_in_build", true, true);
    expect(within(ins).getByTestId("rules-found-bug-absent")).toHaveTextContent("default, fix_in_build, is in force");
  });

  it("offers only the two modes in the editor", async () => {
    const ins = await open("fix_in_build");
    const group = await within(ins).findByRole("radiogroup", { name: "What a builder does with a bug it finds" });
    expect(within(group).getAllByRole("radio")).toHaveLength(2);
  });

  it("saving a mode change requires a reason, and sends it", async () => {
    const user = userEvent.setup();
    vi.mocked(api.setRuleToggles).mockResolvedValue({} as never);
    const ins = await open("fix_in_build");
    const group = await within(ins).findByRole("radiogroup", { name: "What a builder does with a bug it finds" });
    await user.click(within(group).getByRole("radio", { name: /File a ticket/ }));
    expect(within(ins).getByTestId("rules-dock-diff")).toHaveTextContent("file_ticket");
    const save = within(ins).getByTestId("rules-dock-save-button");
    expect(save).toBeDisabled();
    await user.type(within(ins).getByLabelText("Reason"), "bugs found mid-build get their own ticket");
    expect(save).toBeEnabled();
    await user.click(save);
    await waitFor(() => expect(api.setRuleToggles).toHaveBeenCalled());
    const sent = JSON.stringify(vi.mocked(api.setRuleToggles).mock.calls[0]);
    expect(sent).toContain("bugs found mid-build get their own ticket");
    expect(sent).toContain("file_ticket");
  });
});
