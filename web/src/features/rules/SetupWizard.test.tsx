/** Setup wizard (GRPH-1601). */
import type * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { RuleFlowRule, RulesFlow } from "@/lib/types";

import { SetupWizard } from "./SetupWizard";

vi.mock("@/lib/api", () => ({
  api: { setRuleToggles: vi.fn(), ruleToggles: vi.fn(), me: vi.fn(), members: vi.fn(), pathCounts: vi.fn(), exclusiveAreasPreview: vi.fn() },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core" }, projects: [], setActiveId: () => {} }),
}));

function rule(id: string, stage: string, params: Record<string, string | number>): RuleFlowRule {
  return {
    id, stage, title: id, summary: "", enforcement: "enforced", channel: null,
    enforcer: { kind: "code", url: "" }, truth: "db", scope: "project", models: [], value: true,
    source: "platform default", used: true, label: null, skills: null, control: "toggle",
    params: { value: params, source: "platform default" }, locked: false,
  } as unknown as RuleFlowRule;
}
const scope = rule("build.test_scope", "build", { mode: "agent_full" });
const gate = rule("merge.gate", "merge", { min_effort: 3 });
const flow = {
  stages: ["spec", "plan", "build", "review", "merge", "learn", "release"].map((id) => ({
    id, title: id[0].toUpperCase() + id.slice(1), summary: "", state: "enforced",
    rules: id === "build" ? [scope] : id === "merge" ? [gate] : [],
  })),
} as unknown as RulesFlow;

function view(over: { onExit?: () => void; onApplied?: (ids: string[]) => void } = {}) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SetupWizard flow={flow} narrow={false} onExit={over.onExit ?? (() => {})} onApplied={over.onApplied ?? (() => {})} />
    </QueryClientProvider>,
  );
}

async function setMergeEffort(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "4. Merge" }));
  await user.click(screen.getByRole("button", { name: "Set my own" }));
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.me).mockResolvedValue({ id: "u1" } as never);
  vi.mocked(api.members).mockResolvedValue([{ user: { id: "u1" }, role: "owner" }] as never);
});

describe("SetupWizard", () => {
  it("Set my own on done predicates shows the deployment floor and saves it with an addition", async () => {
    const user = userEvent.setup();
    const done = rule("merge.done_predicates", "merge", {});
    (done.params as { value: unknown }).value = { predicates: ["suite_green"] };
    const doneFlow = {
      stages: flow.stages.map((s) => (s.id === "merge" ? { ...s, rules: [done] } : s)),
    } as unknown as RulesFlow;
    vi.mocked(api.ruleToggles).mockResolvedValue({
      toggles: [{
        rule_id: "merge.done_predicates",
        effective_predicates: [{ name: "suite_green", source: "deployment" }, { name: "scans_clean", source: "deployment" }],
      }],
    } as never);
    vi.mocked(api.setRuleToggles).mockResolvedValue({} as never);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <SetupWizard flow={doneFlow} narrow={false} onExit={() => {}} onApplied={() => {}} />
      </QueryClientProvider>,
    );
    await setMergeEffort(user);
    await waitFor(() =>
      expect(screen.getAllByTestId("rules-predicate-floor").map((e) => e.textContent)).toEqual(["suite_green · required", "scans_clean · required"]),
    );
    await user.click(screen.getByRole("button", { name: "Add entry" }));
    await user.type(screen.getByLabelText("additions 1"), "docs_built");
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await user.type(screen.getByLabelText("Reason"), "floor");
    await user.click(screen.getByTestId("rules-review-save"));
    await waitFor(() => expect(api.setRuleToggles).toHaveBeenCalledTimes(1));
    const [, body] = vi.mocked(api.setRuleToggles).mock.calls[0];
    expect(body.toggles[0].params).toEqual({ predicates: ["suite_green", "scans_clean", "docs_built"] });
  });

  it("walks Plan to Learn, skips Spec and Release with reasons, and starts on the default", async () => {
    view();
    expect(screen.getByRole("button", { name: "1. Plan" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "5. Learn" })).toBeInTheDocument();
    expect(screen.getByTestId("rules-wizard-skipped")).toHaveTextContent(/Spec: Skipped/);
    expect(screen.getByTestId("rules-wizard-skipped")).toHaveTextContent(/Release: Skipped/);
    await userEvent.setup().click(screen.getByRole("button", { name: "2. Build" }));
    expect(screen.getByRole("button", { name: "Keep the default" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("rules-wizard-detected")).toHaveTextContent("Nothing detected");
  });

  it("keeps an answer across Back and Next", async () => {
    const user = userEvent.setup();
    view();
    await user.click(screen.getByRole("button", { name: "2. Build" }));
    await user.click(screen.getByRole("button", { name: "Set my own" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByRole("button", { name: "Set my own" })).toHaveAttribute("aria-pressed", "true");
  });

  it("exits without asking when every answer is still the default", async () => {
    const user = userEvent.setup();
    const onExit = vi.fn();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    view({ onExit });
    await user.click(screen.getByTestId("rules-wizard-exit"));
    expect(confirm).not.toHaveBeenCalled();
    expect(onExit).toHaveBeenCalledTimes(1);
    confirm.mockRestore();
  });

  it("asks before Exit discards a changed answer: declining keeps the wizard, accepting leaves", async () => {
    const user = userEvent.setup();
    const onExit = vi.fn();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    view({ onExit });
    await setMergeEffort(user);
    await user.click(screen.getByRole("button", { name: "Increase min_effort" }));
    await user.click(screen.getByTestId("rules-wizard-exit"));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(onExit).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Set my own" })).toHaveAttribute("aria-pressed", "true");
    confirm.mockReturnValue(true);
    await user.click(screen.getByTestId("rules-wizard-exit"));
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(onExit).toHaveBeenCalledTimes(1);
    confirm.mockRestore();
  });

  it("asks before an in-app link outside the wizard discards a changed answer", async () => {
    const user = userEvent.setup();
    const navigated = vi.fn((e: React.MouseEvent) => e.preventDefault());
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <a href="/items" onClick={navigated}>Items</a>
        <SetupWizard flow={flow} narrow={false} onExit={() => {}} onApplied={() => {}} />
      </QueryClientProvider>,
    );
    // Clean: the link goes through with no question.
    await user.click(screen.getByRole("link", { name: "Items" }));
    expect(confirm).not.toHaveBeenCalled();
    expect(navigated).toHaveBeenCalledTimes(1);
    await setMergeEffort(user);
    await user.click(screen.getByRole("button", { name: "Increase min_effort" }));
    // Dirty and declined: the router never sees the click.
    await user.click(screen.getByRole("link", { name: "Items" }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(navigated).toHaveBeenCalledTimes(1);
    // Dirty and accepted: it goes through.
    confirm.mockReturnValue(true);
    await user.click(screen.getByRole("link", { name: "Items" }));
    expect(navigated).toHaveBeenCalledTimes(2);
    confirm.mockRestore();
  });

  it("applies the changed rules in one batch with one reason and reports them", async () => {
    const user = userEvent.setup();
    const onApplied = vi.fn();
    vi.mocked(api.setRuleToggles).mockResolvedValue({} as never);
    view({ onApplied });
    await setMergeEffort(user);
    await user.click(screen.getByRole("button", { name: "Increase min_effort" }));
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await user.type(screen.getByLabelText("Reason"), "first setup");
    await user.click(screen.getByTestId("rules-review-save"));
    await waitFor(() => expect(onApplied).toHaveBeenCalledWith(["merge.gate"]));
    expect(api.setRuleToggles).toHaveBeenCalledTimes(1);
    expect(api.setRuleToggles).toHaveBeenCalledWith("core", {
      toggles: [{ rule_id: "merge.gate", params: { min_effort: 4 } }],
      reason: "first setup",
    });
  });

  it("applies nothing when every answer is the default", async () => {
    const user = userEvent.setup();
    view();
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(screen.getByTestId("rules-wizard-no-changes")).toBeInTheDocument();
    expect(api.setRuleToggles).not.toHaveBeenCalled();
  });

  it("on a refusal returns to the step with the rule and keeps the answers", async () => {
    const user = userEvent.setup();
    vi.mocked(api.setRuleToggles).mockRejectedValue(new Error("merge.gate: org locked"));
    view();
    await setMergeEffort(user);
    await user.click(screen.getByRole("button", { name: "Increase min_effort" }));
    await user.click(screen.getByRole("button", { name: "5. Learn" }));
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await user.type(screen.getByLabelText("Reason"), "because");
    await user.click(screen.getByTestId("rules-review-save"));
    await waitFor(() => expect(screen.getByTestId("rules-wizard-refused-merge.gate")).toHaveTextContent("org locked"));
    expect(screen.getByRole("button", { name: "Set my own" })).toHaveAttribute("aria-pressed", "true");
  });
});
