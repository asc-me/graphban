/** Stage editor and review sheet (GRPH-1599). */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { RuleFlowRule, RuleStage } from "@/lib/types";

import { StageEditor } from "./StageEditor";

vi.mock("@/lib/api", () => ({
  api: {
    setRuleToggles: vi.fn(),
    gitops: vi.fn(),
    updateGitops: vi.fn(),
    me: vi.fn(),
    members: vi.fn(),
    pathCounts: vi.fn(),
    exclusiveAreasPreview: vi.fn(),
  },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core" }, projects: [], setActiveId: () => {} }),
}));

function rule(over: Partial<RuleFlowRule> & { id: string }): RuleFlowRule {
  return {
    stage: "plan", title: over.id, summary: "", enforcement: "enforced", channel: null,
    enforcer: { kind: "code", url: "" } as RuleFlowRule["enforcer"], truth: "db", scope: "project", models: [],
    value: true, source: "platform default", used: true, label: null, skills: null, control: "toggle",
    params: { value: {}, source: "platform default" }, locked: false, ...over,
  } as RuleFlowRule;
}

const areas = rule({ id: "plan.exclusive_areas", title: "Exclusive areas", params: { value: { paths: ["backend/"] }, source: "platform default" } });
const scope = rule({
  id: "build.test_scope", title: "Test scope",
  params: { value: { mode: "agent_full" }, source: "platform default" },
});
const bar = rule({ id: "gitops.reviewer_bar", title: "Reviewer bar", control: null, params: null, value: "sign_off" });
const fixed = rule({ id: "review.not_own_work", title: "Not your own work", control: null, params: null });
const stage: RuleStage = { id: "plan", title: "Plan", summary: "", state: "enforced", rules: [areas, scope, bar, fixed] } as RuleStage;

function view() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <StageEditor stage={stage} narrow={false} onClose={() => {}} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.me).mockResolvedValue({ id: "u1" } as never);
  vi.mocked(api.members).mockResolvedValue([{ user: { id: "u1" }, role: "owner" }] as never);
  vi.mocked(api.pathCounts).mockResolvedValue({ indexed: true, paths: [] });
  vi.mocked(api.exclusiveAreasPreview).mockResolvedValue({ serialised: [{ id: "P74W-1" }, { id: "P74W-2" }] });
  vi.mocked(api.gitops).mockResolvedValue({
    control: { state: "local", writable: true, message: "" },
    fields: { base_branch: { value: "main" }, no_push_to_base: { value: true }, branch_name_pattern: { value: null }, pr_title_pattern: { value: null }, reviewer_bar: { value: "sign_off", source: "project" } },
    version_from: { value: null }, release_defined_in: { value: null }, model: { value: "x" },
  } as never);
});

describe("StageEditor", () => {
  it("collects changes in one draft, shows before → after, asks for one reason and batch-saves", async () => {
    const user = userEvent.setup();
    vi.mocked(api.setRuleToggles).mockResolvedValue({ toggles: [] });
    view();
    expect(screen.getByRole("button", { name: /Review changes/ })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Add path" }));
    await user.type(screen.getByLabelText("paths 2"), "web/");
    await user.click(screen.getByRole("switch", { name: "Test scope: on" }));
    await user.click(screen.getByRole("button", { name: /Review changes · 2/ }));
    const sheet = await screen.findByTestId("rules-review-sheet");
    expect(within(sheet).getByTestId("rules-review-rule-plan.exclusive_areas")).toHaveTextContent("backend/ → backend/, web/");
    expect(within(sheet).getByTestId("rules-review-rule-plan.exclusive_areas")).toHaveTextContent("source: platform default → project");
    await waitFor(() => expect(within(sheet).getByText("P74W-1 and P74W-2 would build one at a time.")).toBeInTheDocument());
    expect(within(sheet).getAllByLabelText("Reason")).toHaveLength(1);
    expect(within(sheet).getByTestId("rules-review-save")).toBeDisabled();
    await user.type(within(sheet).getByLabelText("Reason"), "tighten planning");
    await user.click(within(sheet).getByTestId("rules-review-save"));
    await waitFor(() => expect(api.setRuleToggles).toHaveBeenCalledTimes(1));
    const [, body] = vi.mocked(api.setRuleToggles).mock.calls[0];
    expect(body.reason).toBe("tighten planning");
    expect(body.toggles.map((t) => t.rule_id)).toEqual(["plan.exclusive_areas", "build.test_scope"]);
    expect(body.toggles[0].params).toEqual({ paths: ["backend/", "web/"] });
  });

  it("a refusal reopens the draft with the server's words on the rule, keeping the reason and the draft", async () => {
    const user = userEvent.setup();
    vi.mocked(api.setRuleToggles).mockRejectedValue(
      Object.assign(new Error(JSON.stringify({ detail: "plan.exclusive_areas: a path may not be empty" })), { status: 422 }),
    );
    view();
    await user.click(screen.getByRole("button", { name: "Add path" }));
    await user.type(screen.getByLabelText("paths 2"), "web/");
    await user.click(screen.getByRole("button", { name: /Review changes/ }));
    await user.type(await screen.findByLabelText("Reason"), "because");
    await user.click(screen.getByTestId("rules-review-save"));
    const err = await screen.findByTestId("rules-edit-error-plan.exclusive_areas");
    expect(err).toHaveTextContent("a path may not be empty");
    expect(screen.queryByTestId("rules-review-sheet")).not.toBeInTheDocument();
    expect(screen.getByLabelText("paths 2")).toHaveValue("web/");
    await user.click(screen.getByRole("button", { name: /Review changes/ }));
    expect(await screen.findByLabelText("Reason")).toHaveValue("because");
  });

  it("a risky choice needs the risk confirmed before it saves", async () => {
    const user = userEvent.setup();
    view();
    await user.click(screen.getByRole("radio", { name: "The agent runs partial CI, only what changed" }));
    await user.click(screen.getByRole("button", { name: /Review changes/ }));
    const sheet = await screen.findByTestId("rules-review-sheet");
    await user.type(within(sheet).getByLabelText("Reason"), "faster");
    expect(within(sheet).getByTestId("rules-review-save")).toBeDisabled();
    await user.click(within(sheet).getByRole("checkbox"));
    expect(within(sheet).getByTestId("rules-review-save")).toBeEnabled();
  });

  it("gitops settings are labelled and save through the gitops patch; fixed rules are read-only with their reason", async () => {
    const user = userEvent.setup();
    vi.mocked(api.updateGitops).mockImplementation(() => api.gitops("core"));
    view();
    expect(screen.getByTestId("rules-edit-review.not_own_work")).toHaveTextContent("a builder can't review its own work");
    expect(within(screen.getByTestId("rules-edit-review.not_own_work")).queryByRole("textbox")).toBeNull();
    const field = screen.getByTestId("rules-edit-gitops-reviewer_bar");
    expect(field).toHaveTextContent("GITOPS SETTING");
    const select = await within(field).findByLabelText("Reviewer bar");
    await user.selectOptions(select, "forge");
    await user.click(screen.getByRole("button", { name: /Review changes/ }));
    const sheet = await screen.findByTestId("rules-review-sheet");
    expect(within(sheet).getByTestId("rules-review-gitops-reviewer_bar")).toHaveTextContent("sign_off → forge");
    await user.click(within(sheet).getByTestId("rules-review-save"));
    await waitFor(() => expect(api.updateGitops).toHaveBeenCalledWith("core", { reviewer_bar: "forge" }));
    expect(api.setRuleToggles).not.toHaveBeenCalled();
  });
});
