import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BudgetCapEditor, GitopsRuleEditor, policyWithItemCap } from "@/features/rules/RuleEditors";
import { api } from "@/lib/api";
import type { GitopsView, RuleFlowRule } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  api: { config: vi.fn(), syncStatus: vi.fn(), gitops: vi.fn(), updateGitops: vi.fn(), fleetPolicy: vi.fn(), saveFleetPolicy: vi.fn() },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core", tag: "CORE", name: "Core" }, projects: [], setActiveId: () => {} }),
}));

const rule = { id: "gitops.base_branch" } as RuleFlowRule;
const box = <div>current box</div>;

function view(over: Partial<GitopsView["control"]> = {}, org_id: string | null = null): GitopsView {
  const f = <T,>(value: T) => ({ value, source: "project" as const });
  return {
    project_id: "core", org_id,
    fields: {
      base_branch: f("main"), no_push_to_base: f(null), branch_name_pattern: f(null),
      pr_title_pattern: f(null), reviewer_bar: f(null),
    },
    control: { state: "local", writable: true, message: "", ...over },
    was: null, version_from: f(null), release_defined_in: f(null), model: f(null), plan: null, projects: [],
  };
}

function renderIt(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.config).mockResolvedValue({ hosted_mode: false } as never);
  vi.mocked(api.syncStatus).mockResolvedValue({ linked: true, org: "Acme Co" } as never);
});

describe("gitops row editing", () => {
  it("saves through PATCH and shows ADVISORY copy, never enforced", async () => {
    vi.mocked(api.gitops).mockResolvedValue(view());
    vi.mocked(api.updateGitops).mockResolvedValue(view());
    renderIt(<GitopsRuleEditor rule={rule} field="base_branch" fallback={box} />);
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const input = screen.getByLabelText("base_branch");
    await userEvent.clear(input);
    await userEvent.type(input, "stage");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.updateGitops).toHaveBeenCalledWith("core", { base_branch: "stage" }));
    expect(await screen.findByTestId("rules-gitops-saved")).toHaveTextContent("Nothing refuses a violation");
  });

  it("shows the server 422 verbatim and keeps the typed value", async () => {
    vi.mocked(api.gitops).mockResolvedValue(view());
    vi.mocked(api.updateGitops).mockRejectedValue(new Error("unknown token {bogus}"));
    renderIt(<GitopsRuleEditor rule={rule} field="branch_name_pattern" fallback={box} />);
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.type(screen.getByLabelText("branch_name_pattern"), "x/{{bogus}");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("unknown token {bogus}");
    expect(screen.getByLabelText("branch_name_pattern")).toHaveValue("x/{bogus}");
  });

  it("renders no input when not writable, with the org-aware copy", async () => {
    vi.mocked(api.gitops).mockResolvedValue(view({ writable: false }, "org1"));
    renderIt(<GitopsRuleEditor rule={rule} field="base_branch" fallback={box} />);
    expect(await screen.findByTestId("rules-gitops-readonly")).toHaveTextContent("Only an org owner or admin can change this");
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("uses the project copy for a non-org project", async () => {
    vi.mocked(api.gitops).mockResolvedValue(view({ writable: false }));
    renderIt(<GitopsRuleEditor rule={rule} field="base_branch" fallback={box} />);
    expect(await screen.findByTestId("rules-gitops-readonly")).toHaveTextContent("You don't have write access to this project");
  });

  it("linked rows never open an editor", async () => {
    vi.mocked(api.gitops).mockResolvedValue(view({ state: "linked_set", writable: false }, "acme"));
    const { unmount } = renderIt(<GitopsRuleEditor rule={rule} field="base_branch" fallback={box} />);
    expect(await screen.findByTestId("rules-gitops-linked")).toHaveTextContent("ORG · Acme Co");
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    unmount();
    vi.mocked(api.gitops).mockResolvedValue(view({ state: "linked_unset", writable: false }, "acme"));
    renderIt(<GitopsRuleEditor rule={rule} field="base_branch" fallback={box} />);
    expect(await screen.findByText("Acme Co has not set this")).toBeTruthy();
  });

  it("unreachable shows UNMEASURED with no value and no input", async () => {
    vi.mocked(api.gitops).mockResolvedValue(view({ state: "linked_unreachable", writable: false, message: "Org unreachable" }));
    renderIt(<GitopsRuleEditor rule={rule} field="base_branch" fallback={box} />);
    expect(await screen.findByTestId("rules-gitops-unreachable")).toHaveTextContent("UNMEASURED");
    expect(screen.queryByText("current box")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});

describe("budget cap editing", () => {
  const stored = {
    local_only: true, reviewer_cross_vendor: true, allowed_harnesses: ["claude"],
    caps: { per_attempt_tokens: 5, per_item_tokens: 100, per_period_tokens: 900, period: "week" as const },
    cluster_ceiling: { max: 3 },
  };

  it("a cap edit sends every other policy key unchanged", async () => {
    vi.mocked(api.fleetPolicy).mockResolvedValue({ project_id: "core", policy: stored });
    vi.mocked(api.saveFleetPolicy).mockResolvedValue({ project_id: "core", policy: stored });
    renderIt(<BudgetCapEditor />);
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const input = screen.getByLabelText("per_item_tokens");
    await userEvent.clear(input);
    await userEvent.type(input, "250");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.saveFleetPolicy).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.saveFleetPolicy).mock.calls[0][0]).toEqual({
      project_id: "core",
      ...stored,
      caps: { ...stored.caps, per_item_tokens: 250 },
    });
  });

  it("with writable false there is no Edit button or input, only the read-only copy", async () => {
    vi.mocked(api.fleetPolicy).mockResolvedValue({ project_id: "core", policy: stored, writable: false });
    renderIt(<BudgetCapEditor />);
    expect(await screen.findByText("100 tokens per item")).toBeTruthy();
    expect(screen.getByText("You don't have write access to this project")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.queryByLabelText("per_item_tokens")).toBeNull();
  });

  it("policyWithItemCap with no stored policy sends defaults plus the cap", () => {
    expect(policyWithItemCap(null, 7)).toEqual({
      local_only: false, reviewer_cross_vendor: false, allowed_harnesses: [], caps: { per_item_tokens: 7 },
    });
  });
});


describe("linked gitops rows", () => {
  const linkedView = (state: "linked_set" | "linked_unset" | "linked_unreachable") => {
    const v = view({ state, writable: false }, "org_1");
    if (state === "linked_set") v.fields.base_branch = { value: "trunk", source: "org" };
    return v;
  };

  it("linked_set keeps the value box and names the org, not its id", async () => {
    vi.mocked(api.gitops).mockResolvedValue(linkedView("linked_set"));
    renderIt(<GitopsRuleEditor rule={rule} field="base_branch" fallback={box} />);
    expect(await screen.findByTestId("rules-gitops-org")).toHaveTextContent("ORG · Acme Co");
    expect(screen.getByText("current box")).toBeInTheDocument();
    expect(screen.queryByText(/org_1/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
  });

  it("linked_unset says the org has not set it", async () => {
    vi.mocked(api.gitops).mockResolvedValue(linkedView("linked_unset"));
    renderIt(<GitopsRuleEditor rule={rule} field="base_branch" fallback={box} />);
    expect(await screen.findByTestId("rules-gitops-org")).toHaveTextContent("Acme Co has not set this");
  });

  it("linked_unreachable shows no value and no input", async () => {
    vi.mocked(api.gitops).mockResolvedValue(linkedView("linked_unreachable"));
    renderIt(<GitopsRuleEditor rule={rule} field="base_branch" fallback={box} />);
    expect(await screen.findByTestId("rules-gitops-unreachable")).toBeInTheDocument();
    expect(screen.queryByText("current box")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("self-host Admin link leaves for the console; hosted keeps the in-app route", async () => {
    vi.mocked(api.gitops).mockResolvedValue(linkedView("linked_set"));
    const { unmount } = renderIt(<GitopsRuleEditor rule={rule} field="base_branch" fallback={box} />);
    const selfHost = await screen.findByRole("link", { name: "Admin → Gitops" });
    expect(selfHost.getAttribute("href")).toMatch(/^https?:\/\/.+\/org\/admin\/gitops$/);
    unmount();
    vi.mocked(api.config).mockResolvedValue({ hosted_mode: true } as never);
    renderIt(<GitopsRuleEditor rule={rule} field="base_branch" fallback={box} />);
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "Admin → Gitops" }).getAttribute("href")).toBe("/org/admin/gitops"),
    );
  });
});
