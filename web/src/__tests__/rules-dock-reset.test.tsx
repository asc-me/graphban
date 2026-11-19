/**
 * GRPH-1727: "Reset to inherited" in the rules dock draws the inherited value (the toggles read's
 * `inherited.params`), marks it as inherited, and an edit after the reset starts from it. The
 * p74-walk step: the project set `paths=["client/generated/"]`; reset, add `client/generated/`,
 * save → the 9 platform defaults plus `client/generated/`, in one batch write.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DockEditor } from "@/features/rules/DockEditor";
import { GENERATED_FILES_DEFAULTS } from "@/features/rules/RuleValueEditor";
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

const DEFAULTS = [
  ".agents/skills/", ".claude/agents/", ".claude/skills/", ".codex/agents/", ".cursor/agents/",
  ".cursor/plugins/graphban/", ".cursor/rules/graphban.mdc", "AGENTS.md", "backend/tests/shard_weights.json",
];

const generated = {
  id: "build.generated_files", stage: "build", title: "Generated files", summary: "", enforcement: "enforced",
  channel: null, enforcer: { kind: "graphban_source", ref: "a.py", url: "" }, truth: "db", scope: "project",
  models: ["prs_to_base"], value: true, source: "project", used: true, label: null, skills: null, control: "toggle",
  params: { value: { paths: ["client/generated/"] }, source: "project" }, locked: false,
} as unknown as RuleFlowRule;
const stage = { id: "build", title: "Build", summary: "", state: "enforced", rules: [generated] } as unknown as RuleStage;

const walkToggle = {
  rule_id: "build.generated_files", stage: "build", title: "Generated files",
  on: { value: true, source: "platform default" },
  params: { value: { paths: ["client/generated/"] }, source: "project" },
  inherits: { on: "platform default", params: "platform default" },
  inherited: { on: true, params: { paths: DEFAULTS } },
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
  vi.mocked(api.ruleToggles).mockResolvedValue({ library_loaded: true, toggles: [walkToggle] });
  vi.mocked(api.setRuleToggles).mockResolvedValue({ toggles: [] });
  vi.mocked(api.gitops).mockResolvedValue({
    control: { state: "local", writable: true, message: "" },
    fields: { base_branch: { value: "main" }, no_push_to_base: { value: true }, branch_name_pattern: { value: null }, pr_title_pattern: { value: null }, reviewer_bar: { value: "sign_off", source: "project" } },
    version_from: { value: null }, release_defined_in: { value: null }, model: { value: "x" },
  } as never);
});

describe("rules dock: reset to inherited", () => {
  it("the client's defaults match the walk's platform defaults", () => {
    expect(GENERATED_FILES_DEFAULTS).toEqual(DEFAULTS);
  });

  it("reset draws the inherited value, marked inherited, and the diff names real values", async () => {
    const user = userEvent.setup();
    dock();
    await user.click(await screen.findByTestId("rules-dock-reset-paths"));
    // The editor now shows the defaults (add mode, nothing extra), not `client/generated/`.
    const row = screen.getByTestId("rules-dock-param-wide-paths");
    expect(within(row).getByLabelText("Add to defaults")).toBeChecked();
    expect(within(row).getByTestId("rules-generated-defaults")).toHaveTextContent("AGENTS.md");
    expect(within(row).queryByDisplayValue("client/generated/")).toBeNull();
    expect(screen.getByTestId("rules-dock-inherited-paths")).toHaveTextContent("inherited from the platform default");
    const diff = screen.getByTestId("rules-dock-diff");
    expect(diff).toHaveTextContent("client/generated/");
    expect(diff).toHaveTextContent("backend/tests/shard_weights.json");
    expect(diff).not.toHaveTextContent("project values");

    await user.type(screen.getByLabelText("Reason"), "back to defaults");
    await user.click(screen.getByTestId("rules-dock-save-button"));
    await waitFor(() => expect(api.setRuleToggles).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.setRuleToggles).mock.calls[0][1].toggles[0].params).toBeNull();
  });

  it("reset, add client/generated/, save sends the 9 defaults plus it in one batch write", async () => {
    const user = userEvent.setup();
    dock();
    await user.click(await screen.findByTestId("rules-dock-reset-paths"));
    await user.click(screen.getByRole("button", { name: "Add path" }));
    await user.type(screen.getByLabelText("paths 1"), "client/generated/");
    // The reset is kept: the row still says it starts from the inherited value.
    expect(screen.getByTestId("rules-dock-inherited-paths")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Reason"), "defaults plus the client");
    await user.click(screen.getByTestId("rules-dock-save-button"));
    await waitFor(() => expect(api.setRuleToggles).toHaveBeenCalledTimes(1));
    const [, body] = vi.mocked(api.setRuleToggles).mock.calls[0];
    expect(body.toggles).toHaveLength(1);
    expect(body.toggles[0].params).toEqual({ paths: [...DEFAULTS, "client/generated/"] });
  });
});
