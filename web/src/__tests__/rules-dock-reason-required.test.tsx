/** GRPH-1741: the rules dock says the Why is required and why Save is disabled. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DockEditor } from "@/features/rules/DockEditor";
import { api } from "@/lib/api";
import type { RuleFlowRule, RuleStage, RuleToggle } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  api: {
    rulesFlow: vi.fn(), gitops: vi.fn(), config: vi.fn(), syncStatus: vi.fn(), events: vi.fn(),
    ruleToggles: vi.fn(), setRuleToggles: vi.fn(), ruleEffect: vi.fn(), me: vi.fn(), members: vi.fn(),
    pathCounts: vi.fn(), exclusiveAreasPreview: vi.fn(), updateGitops: vi.fn(),
  },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core", tag: "CORE", name: "Core" }, projects: [], setActiveId: () => {} }),
}));

const rule = {
  id: "build.generated_files", stage: "build", title: "Generated files", summary: "", enforcement: "enforced",
  channel: null, enforcer: { kind: "graphban_source", ref: "a.py", url: "" }, truth: "db", scope: "project",
  models: ["prs_to_base"], value: true, source: "project", used: true, label: null, skills: null, control: "toggle",
  params: { value: { paths: ["client/generated/"] }, source: "project" }, locked: false,
} as unknown as RuleFlowRule;
const stage = { id: "build", title: "Build", summary: "", state: "enforced", rules: [rule] } as unknown as RuleStage;
const toggle = {
  rule_id: "build.generated_files", stage: "build", title: "Generated files",
  on: { value: true, source: "platform default" },
  params: { value: { paths: ["client/generated/"] }, source: "project" },
  inherits: { on: "platform default", params: "platform default" },
  inherited: { on: true, params: { paths: ["AGENTS.md"] } },
  locked: false, locked_by: null,
} as RuleToggle;

function dock() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <DockEditor stage={stage} version="1.0.0" model="prs_to_base" focusRule={null} prefill={null} onClose={() => {}} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.me).mockResolvedValue({ id: "u1" } as never);
  vi.mocked(api.members).mockResolvedValue([{ user: { id: "u1" }, role: "owner" }] as never);
  vi.mocked(api.pathCounts).mockResolvedValue({ indexed: true, paths: [] });
  vi.mocked(api.exclusiveAreasPreview).mockResolvedValue({ serialised: [] });
  vi.mocked(api.config).mockResolvedValue({ hosted_mode: false } as never);
  vi.mocked(api.syncStatus).mockResolvedValue({ linked: false, org: "" } as never);
  vi.mocked(api.events).mockResolvedValue({ results: [], total: 0 } as never);
  vi.mocked(api.ruleEffect).mockRejectedValue(new Error("no effect"));
  vi.mocked(api.ruleToggles).mockResolvedValue({ library_loaded: true, toggles: [toggle] });
  vi.mocked(api.setRuleToggles).mockResolvedValue({ toggles: [] });
  vi.mocked(api.gitops).mockResolvedValue({
    control: { state: "local", writable: true, message: "" },
    fields: { base_branch: { value: "main" }, no_push_to_base: { value: true }, branch_name_pattern: { value: null }, pr_title_pattern: { value: null }, reviewer_bar: { value: "sign_off", source: "project" } },
    version_from: { value: null }, release_defined_in: { value: null }, model: { value: "x" },
  } as never);
});

async function dirtyDock() {
  const user = userEvent.setup();
  dock();
  await user.click(await screen.findByTestId("rules-dock-reset-paths"));
  return user;
}

describe("rules dock: the Why is required", () => {
  it("empty reason: visible label, hint names the reason, Save disabled", async () => {
    await dirtyDock();
    expect(screen.getByTestId("rules-dock-reason-label")).toHaveTextContent("Why (required)");
    const input = screen.getByLabelText("Reason");
    expect(input).toHaveAttribute("aria-required", "true");
    const hint = screen.getByTestId("rules-dock-reason-hint");
    expect(hint).toHaveTextContent("Add a reason (8+ characters) to save");
    expect(input).toHaveAttribute("aria-describedby", hint.id);
    const save = screen.getByTestId("rules-dock-save-button");
    expect(save).toBeDisabled();
    expect(save).toHaveAttribute("title", hint.textContent);
  });

  it("short reason: hint still shows and says how many more are needed", async () => {
    const user = await dirtyDock();
    await user.type(screen.getByLabelText("Reason"), "abc");
    expect(screen.getByTestId("rules-dock-reason-hint")).toHaveTextContent("5 more needed");
    expect(screen.getByTestId("rules-dock-save-button")).toBeDisabled();
  });

  it("ok reason: hint gone, Save enabled", async () => {
    const user = await dirtyDock();
    await user.type(screen.getByLabelText("Reason"), "long enough");
    expect(screen.queryByTestId("rules-dock-reason-hint")).toBeNull();
    expect(screen.getByLabelText("Reason")).not.toHaveAttribute("aria-describedby");
    expect(screen.getByTestId("rules-dock-save-button")).toBeEnabled();
  });

  it("clean dock shows no hint", async () => {
    dock();
    await screen.findByTestId("rules-dock-reset-paths");
    expect(screen.queryByTestId("rules-dock-reason-hint")).toBeNull();
  });
});
