/**
 * The floating docs trigger sat on the rules dock's pinned Save row (GRPH-1729): it is hidden
 * while a stage is open in the dock and back when the dock closes. The "?" shortcut still opens
 * the docs; the agent-rail hiding (GRPH-923) and the planner-route offset are unchanged.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DocsReader } from "@/features/docs/DocsReader";
import { RulesView } from "@/features/rules/RulesView";
import { api } from "@/lib/api";
import type { RuleFlowRule, RuleStage, RulesFlow } from "@/lib/types";

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

const TRIGGER = "Open docs for this page";

const generated = {
  id: "build.generated_files", stage: "build", title: "Generated files", summary: "s", enforcement: "enforced",
  channel: null, enforcer: { kind: "graphban_source", ref: "a.py", url: "" }, truth: "db", scope: "project",
  models: ["prs_to_base"], value: true, source: "project", used: true, label: null, skills: null, control: "toggle",
  params: { value: { paths: ["a.lock"] }, source: "project" }, locked: false,
} as unknown as RuleFlowRule;

const flow = {
  version: "1.0.0", model: "prs_to_base",
  control: { state: "local", writable: true, message: "", org_name: "" },
  edges: [],
  stages: [{ id: "build", title: "Build", summary: "", state: "enforced", rules: [generated] } as unknown as RuleStage],
} as unknown as RulesFlow;

function shell(url: string, agentOpen = false) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[url]}>
        <RulesView />
        <DocsReader agentOpen={agentOpen} />
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

describe("docs bubble and the rules dock (GRPH-1729)", () => {
  it("docs trigger is hidden while a stage is open in the rules dock and back when it closes", async () => {
    const user = userEvent.setup();
    shell("/rules?stage=build&rule=build.generated_files");
    await screen.findByTestId("rules-inspector");
    expect(screen.queryByLabelText(TRIGGER)).not.toBeInTheDocument();
    await user.click(screen.getByTestId("rules-dock-close"));
    await waitFor(() => expect(screen.queryByTestId("rules-inspector")).not.toBeInTheDocument());
    expect(screen.getByLabelText(TRIGGER)).toBeInTheDocument();
  });

  it("docs trigger shows on the rules map with the dock closed", async () => {
    shell("/rules");
    await screen.findByTestId("rules-map-area");
    expect(screen.getByLabelText(TRIGGER)).toBeInTheDocument();
  });

  it("docs trigger comes back when the rules view unmounts with the dock open", async () => {
    const first = shell("/rules?stage=build");
    await screen.findByTestId("rules-inspector");
    first.unmount();
    render(
      <MemoryRouter initialEntries={["/dashboard"]}>
        <DocsReader />
      </MemoryRouter>,
    );
    expect(screen.getByLabelText(TRIGGER)).toBeInTheDocument();
  });

  it("the ? shortcut still opens the docs sheet while the rules dock is open", async () => {
    shell("/rules?stage=build");
    await screen.findByTestId("rules-inspector");
    expect(screen.queryByLabelText(TRIGGER)).not.toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "?" });
    expect(await screen.findByRole("dialog", { name: "Docs" })).toBeInTheDocument();
  });

  it("agent rail still hides the trigger and planner routes keep the raised offset", () => {
    const rail = render(
      <MemoryRouter initialEntries={["/tracker"]}>
        <DocsReader agentOpen />
      </MemoryRouter>,
    );
    expect(screen.queryByLabelText(TRIGGER)).not.toBeInTheDocument();
    rail.unmount();
    const planner = render(
      <MemoryRouter initialEntries={["/tracker"]}>
        <DocsReader />
      </MemoryRouter>,
    );
    expect(screen.getByLabelText(TRIGGER)).toHaveClass("bottom-24");
    planner.unmount();
    render(
      <MemoryRouter initialEntries={["/dashboard"]}>
        <DocsReader />
      </MemoryRouter>,
    );
    expect(screen.getByLabelText(TRIGGER)).toHaveClass("bottom-6");
  });
});
