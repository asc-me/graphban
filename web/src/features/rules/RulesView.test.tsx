/** The Rules page opens the setup wizard and, after Apply, lands on the map with the changed rules highlighted (GRPH-1601). */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { RuleFlowRule, RulesFlow } from "@/lib/types";

import { RulesView } from "./RulesView";

vi.mock("@/lib/api", () => ({
  api: {
    rulesFlow: vi.fn(), gitops: vi.fn(), config: vi.fn(), syncStatus: vi.fn(),
    setRuleToggles: vi.fn(), me: vi.fn(), members: vi.fn(), pathCounts: vi.fn(), exclusiveAreasPreview: vi.fn(),
    ruleProfiles: vi.fn(), ruleProfileDrift: vi.fn(), ruleProfileUpdates: vi.fn(), applyRuleProfile: vi.fn(),
    acceptRuleProfileUpdate: vi.fn(),
  },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core", tag: "CORE", name: "Core" }, projects: [], setActiveId: () => {} }),
}));

function rule(id: string, stage: string, params: Record<string, number> | null): RuleFlowRule {
  return {
    id, stage, title: id, summary: "", enforcement: "enforced", channel: null,
    enforcer: { kind: "graphban_source", ref: "", url: "" }, truth: "db", scope: "project", models: ["prs_to_base"],
    value: true, source: "platform default", used: true, label: null, skills: null,
    control: params ? "toggle" : null, params: params ? { value: params, source: "platform default" } : null, locked: false,
  } as unknown as RuleFlowRule;
}

const flow = {
  version: "1.0.0",
  model: "prs_to_base",
  control: { state: "local", writable: true, message: "", org_name: "" },
  edges: [["spec", "plan"], ["plan", "build"], ["build", "review"], ["review", "merge"], ["merge", "release"], ["release", "learn"]],
  stages: ["spec", "plan", "build", "review", "merge", "release", "learn"].map((id) => ({
    id, title: id[0].toUpperCase() + id.slice(1), summary: "", state: "enforced",
    rules: id === "merge" ? [rule("merge.gate", "merge", { min_effort: 3 }), rule("merge.other", "merge", null)] : [],
  })),
} as unknown as RulesFlow;

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.rulesFlow).mockResolvedValue(flow);
  vi.mocked(api.gitops).mockResolvedValue({ control: { state: "local", writable: true, message: "" } } as never);
  vi.mocked(api.config).mockResolvedValue({ hosted_mode: false } as never);
  vi.mocked(api.syncStatus).mockResolvedValue({ linked: false } as never);
  vi.mocked(api.me).mockResolvedValue({ id: "u1" } as never);
  vi.mocked(api.members).mockResolvedValue([{ user: { id: "u1" }, role: "owner" }] as never);
  vi.mocked(api.setRuleToggles).mockResolvedValue({} as never);
  vi.mocked(api.ruleProfiles).mockResolvedValue({
    project_id: "core", pack_loaded: true, pack_version: "0.4.0", order: ["standard"], applied: null,
    profiles: [{ id: "standard", title: "Standard", summary: "", version: "0.1.0",
      pins: [{ rule_id: "merge.gate", state: "on", params: { min_effort: 5 }, enforceable: true }] }],
  });
  vi.mocked(api.ruleProfileUpdates).mockResolvedValue({ project_id: "core", updates: [] });
});

describe("RulesView setup wizard", () => {
  it("Set up rules opens the wizard; Apply lands on the map with the changed rule highlighted", async () => {
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/rules"]}>
          <RulesView />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await user.click(await screen.findByTestId("rules-open-wizard"));
    expect(screen.getByTestId("rules-setup-wizard")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "4. Merge" }));
    await user.click(screen.getByRole("button", { name: "Set my own" }));
    await user.click(screen.getByRole("button", { name: "Increase min_effort" }));
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await user.type(screen.getByLabelText("Reason"), "first setup");
    await user.click(screen.getByTestId("rules-review-save"));
    await waitFor(() => expect(screen.queryByTestId("rules-setup-wizard")).toBeNull());
    expect(screen.getByTestId("rules-chip-merge.gate")).toHaveAttribute("data-changed", "true");
    expect(screen.getByTestId("rules-chip-merge.other")).not.toHaveAttribute("data-changed");
  });

  it("Quick configure applies a level and lands on the map with the changed rule highlighted", async () => {
    const user = userEvent.setup();
    vi.mocked(api.applyRuleProfile).mockResolvedValue({ project_id: "core", profile: "standard", applied: ["merge.gate"], excepted: [], skipped_locked: [] });
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/rules"]}>
          <RulesView />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await user.click(await screen.findByTestId("rules-open-quick"));
    await user.click(await screen.findByTestId("rules-quick-choose-standard"));
    expect(screen.getByTestId("rules-quick-cell-change-merge.gate")).toBeInTheDocument();
    await user.click(screen.getByTestId("rules-quick-review"));
    await user.type(screen.getByLabelText("Reason"), "baseline");
    await user.click(screen.getByTestId("rules-review-save"));
    await waitFor(() => expect(screen.queryByTestId("rules-quick-configure")).toBeNull());
    expect(api.applyRuleProfile).toHaveBeenCalledWith("core", "standard", { reason: "applying Standard 0.1.0: baseline", except: [], skip_locked: false });
    expect(screen.getByTestId("rules-chip-merge.gate")).toHaveAttribute("data-changed", "true");
  });

  async function openDirtyWizard(user: ReturnType<typeof userEvent.setup>) {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/rules"]}>
          <RulesView />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await user.click(await screen.findByTestId("rules-open-wizard"));
    await user.click(screen.getByRole("button", { name: "4. Merge" }));
    await user.click(screen.getByRole("button", { name: "Set my own" }));
    await user.click(screen.getByRole("button", { name: "Increase min_effort" }));
  }

  it("asks before Quick configure discards a changed answer: declining keeps the wizard, accepting opens Quick configure", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await openDirtyWizard(user);
    await user.click(screen.getByTestId("rules-open-quick"));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("rules-setup-wizard")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set my own" })).toHaveAttribute("aria-pressed", "true");
    confirm.mockReturnValue(true);
    await user.click(screen.getByTestId("rules-open-quick"));
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId("rules-setup-wizard")).toBeNull();
    expect(screen.getByTestId("rules-quick-configure")).toBeInTheDocument();
    confirm.mockRestore();
  });

  it("opens Quick configure without asking when no answer was changed", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/rules"]}>
          <RulesView />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await user.click(await screen.findByTestId("rules-open-wizard"));
    await user.click(screen.getByTestId("rules-open-quick"));
    expect(confirm).not.toHaveBeenCalled();
    expect(screen.getByTestId("rules-quick-configure")).toBeInTheDocument();
    confirm.mockRestore();
  });

  it("asks on browser Back with a changed answer: declining keeps the wizard, accepting leaves", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await openDirtyWizard(user);
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("rules-setup-wizard")).toBeInTheDocument();
    confirm.mockReturnValue(true);
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(confirm).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.queryByTestId("rules-setup-wizard")).toBeNull());
    confirm.mockRestore();
  });
});
