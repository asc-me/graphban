/** GRPH-1756: leaving a rule out (an exception) is visible in the compare table and the review sheet. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { RuleFlowRule, RuleProfiles, RulesFlow } from "@/lib/types";

import { QuickConfigure, type QuickScreen } from "@/features/rules/QuickConfigure";

vi.mock("@/lib/api", () => ({
  api: {
    ruleProfiles: vi.fn(), ruleProfileDrift: vi.fn(), ruleProfileUpdates: vi.fn(),
    applyRuleProfile: vi.fn(), acceptRuleProfileUpdate: vi.fn(),
    me: vi.fn(), members: vi.fn(), pathCounts: vi.fn(), exclusiveAreasPreview: vi.fn(),
  },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core" }, projects: [], setActiveId: () => {} }),
}));

function rule(id: string, stage: string, on: boolean, params: Record<string, string | number> = {}, locked = false): RuleFlowRule {
  return {
    id, stage, title: `T ${id}`, summary: "", enforcement: "enforced", channel: null,
    enforcer: { kind: "code", url: "" }, truth: "db", scope: "project", models: [], value: on,
    source: "platform default", used: true, label: null, skills: null, control: "toggle",
    params: { value: params, source: "platform default" }, locked,
  } as unknown as RuleFlowRule;
}

// Now: sabotage on with 1 test, cross-vendor off, gap-blocks on (locked by the org).
const flow = {
  stages: ["spec", "plan", "build", "review", "merge", "release", "learn"].map((id) => ({
    id, title: id[0].toUpperCase() + id.slice(1), summary: "", state: "enforced",
    rules:
      id === "review"
        ? [
            rule("review.sabotage", "review", true, { min_tests_failed: 1 }),
            rule("review.cross_vendor", "review", false),
            rule("review.gap_blocks", "review", true, {}, true),
          ]
        : [],
  })),
} as unknown as RulesFlow;

const pin = (rule_id: string, state: "on" | "off", params = {}) => ({ rule_id, state, params, enforceable: true });
const profiles: RuleProfiles = {
  project_id: "core", pack_loaded: true, pack_version: "0.4.0", order: ["exploratory", "standard", "regulated"],
  profiles: [
    { id: "exploratory", title: "Exploratory", summary: "Speed.", version: "0.1.0",
      pins: [pin("review.sabotage", "off"), pin("review.cross_vendor", "off"), pin("review.gap_blocks", "off")] },
    { id: "standard", title: "Standard", summary: "Most teams.", version: "0.1.0",
      pins: [pin("review.sabotage", "on", { min_tests_failed: 1 }), pin("review.cross_vendor", "off"), pin("review.gap_blocks", "on")] },
    { id: "regulated", title: "Regulated", summary: "Every change checked.", version: "0.1.0",
      pins: [pin("review.sabotage", "on", { min_tests_failed: 2 }), pin("review.cross_vendor", "on"), pin("review.gap_blocks", "on")] },
  ],
  applied: null,
};

function wrap(ui: React.ReactNode) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);
}
function panel(start: QuickScreen = { screen: "picker" }, onApplied = vi.fn(), narrow = false) {
  return wrap(<QuickConfigure flow={flow} narrow={narrow} orgName="acme" start={start} onExit={() => {}} onApplied={onApplied} onSetup={() => {}} />);
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.ruleProfiles).mockResolvedValue(profiles);
  vi.mocked(api.ruleProfileUpdates).mockResolvedValue({ project_id: "core", updates: [] });
  vi.mocked(api.me).mockResolvedValue({ id: "u1" } as never);
  vi.mocked(api.members).mockResolvedValue([{ user: { id: "u1" }, role: "owner" }] as never);
});

describe("quick configure exception control", () => {
  it("compare: Keep current on a changing row sends it as an exception; unchanged and locked rows have none", async () => {
    const user = userEvent.setup();
    vi.mocked(api.applyRuleProfile).mockResolvedValue({ project_id: "core", profile: "regulated", applied: ["review.sabotage"], excepted: ["review.cross_vendor"], skipped_locked: [] });
    panel({ screen: "compare", profile: "regulated" });
    const keep = await screen.findByTestId("rules-quick-keep-review.cross_vendor");
    expect(keep).toHaveTextContent("Keep current (exception)");
    expect(screen.queryByTestId("rules-quick-keep-review.gap_blocks")).toBeNull();
    expect(screen.getByTestId("rules-quick-summary")).toHaveTextContent("recorded as an exception");
    expect(screen.getByTestId("rules-quick-summary")).toHaveTextContent("2 rules would change");
    await user.click(keep);
    expect(screen.getByTestId("rules-quick-summary")).toHaveTextContent("1 rule would change");
    expect(screen.getByTestId("rules-quick-row-review.cross_vendor")).not.toHaveAttribute("data-changes");
    expect(screen.getByText("kept: exception")).toBeInTheDocument();
    await user.click(screen.getByTestId("rules-quick-review"));
    const sheet = screen.getByTestId("rules-review-sheet");
    expect(within(sheet).queryByTestId("rules-review-rule-review.cross_vendor")).toBeNull();
    expect(within(sheet).getByTestId("rules-quick-dropped")).toHaveTextContent("T review.cross_vendor");
    await user.type(within(sheet).getByLabelText("Reason"), "why");
    await user.click(within(sheet).getByTestId("rules-review-save"));
    await waitFor(() => expect(api.applyRuleProfile).toHaveBeenCalled());
    expect(vi.mocked(api.applyRuleProfile).mock.calls[0][2]).toMatchObject({ except: ["review.cross_vendor"] });
  });

  it("compare: a kept row can be restored from the same control", async () => {
    const user = userEvent.setup();
    panel({ screen: "compare", profile: "regulated" });
    const keep = await screen.findByTestId("rules-quick-keep-review.cross_vendor");
    await user.click(keep);
    expect(screen.getByTestId("rules-quick-summary")).toHaveTextContent("1 rule would change");
    const restore = screen.getByTestId("rules-quick-keep-review.cross_vendor");
    expect(restore).toHaveTextContent("Restore");
    expect(restore).toHaveAttribute("aria-pressed", "true");
    await user.click(restore);
    expect(screen.getByTestId("rules-quick-summary")).toHaveTextContent("2 rules would change");
    expect(screen.getByTestId("rules-quick-row-review.cross_vendor")).toHaveAttribute("data-changes", "true");
    expect(screen.queryByText("kept: exception")).toBeNull();
    expect(screen.getByTestId("rules-quick-keep-review.cross_vendor")).toHaveTextContent("Keep current (exception)");
  });

  it("an unchanged row has no control", async () => {
    panel({ screen: "compare", profile: "standard" });
    await screen.findByTestId("rules-quick-row-review.sabotage");
    expect(screen.queryByTestId("rules-quick-keep-review.sabotage")).toBeNull();
  });

  it("review sheet: the drop control is a labelled button that drops the rule", async () => {
    const user = userEvent.setup();
    panel({ screen: "compare", profile: "regulated" });
    await user.click(await screen.findByTestId("rules-quick-review"));
    const sheet = screen.getByTestId("rules-review-sheet");
    const drop = within(sheet).getByTestId("rules-review-drop-review.sabotage");
    expect(drop).toHaveTextContent("Keep current value (exception)");
    expect(drop.className).toContain("border");
    await user.click(drop);
    expect(within(sheet).queryByTestId("rules-review-rule-review.sabotage")).toBeNull();
  });
});
