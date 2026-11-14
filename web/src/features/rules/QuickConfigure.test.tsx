/** Quick configure (GRPH-1600): picker, compare, review, applied chip, drift, and pack update. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { RuleFlowRule, RuleProfiles, RulesFlow } from "@/lib/types";

import { chipText, planFor, weakeningOf, QuickConfigure, QuickConfigureBar, ruleIndex, type QuickScreen } from "./QuickConfigure";

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

describe("planFor", () => {
  it("lists what would change, skips the org-locked rule, and marks turning a rule off as weakening", () => {
    const rules = ruleIndex(flow);
    const exp = planFor(profiles.profiles[0], rules, new Set());
    expect(exp.changes.map((c) => c.ruleId)).toEqual(["review.sabotage"]);
    expect(exp.changes[0].risk).toMatch(/WEAKENING: turns/);
    expect(exp.locked).toEqual(["review.gap_blocks"]);
    const reg = planFor(profiles.profiles[2], rules, new Set(["review.cross_vendor"]));
    expect(reg.changes.map((c) => c.ruleId)).toEqual(["review.sabotage"]);
    expect(reg.changes[0].params).toEqual([{ name: "min_tests_failed", before: 1, after: 2 }]);
    expect(reg.changes[0].risk).toBeUndefined();
    // gap-blocks is locked on and Regulated pins it on: it already matches, yet the server still
    // counts it as locked and refuses without skip_locked, so the plan names it.
    expect(reg.locked).toEqual(["review.gap_blocks"]);
    expect(planFor(profiles.profiles[2], rules, new Set(["review.gap_blocks"])).locked).toEqual([]);
  });

  it("names the level, exceptions, skipped rules and drift on the chip", () => {
    const applied = { profile: "regulated", version: "0.1.0", pack_version: "0.4.0", exceptions: ["a"], skipped: [], by: "u", at: "" };
    expect(chipText(applied, "Regulated", null)).toBe("Level: Regulated v0.1.0 · 1 exception");
    expect(chipText(applied, "Regulated", 2)).toBe("Level: Regulated v0.1.0 · 1 exception · drifted (2)");
  });
});

describe("weakeningOf lists", () => {
  const rule = { id: "merge.done_predicates", title: "Done predicates", value: true, params: { value: { predicates: ["suite_green", "scans_clean"] } } } as never;
  const pin = (predicates: string[]) => ({ rule_id: "merge.done_predicates", state: "on", params: { predicates } }) as never;
  it("flags a list that drops an entry, names it, and treats an emptied list as the floor", () => {
    expect(weakeningOf(pin(["suite_green"]), rule)).toBe("WEAKENING: predicates drops scans_clean.");
    expect(weakeningOf(pin([]), rule)).toBe("WEAKENING: predicates drops scans_clean.");
    expect(weakeningOf(pin(["suite_green", "scans_clean", "x"]), rule)).toBeUndefined();
  });
});

describe("QuickConfigure", () => {
  it("picker: marks the level the project matches, with version and strictness", async () => {
    panel();
    const card = await screen.findByTestId("rules-quick-card-standard");
    expect(card).toHaveAttribute("data-current", "true");
    expect(card).toHaveTextContent("v0.1.0");
    expect(within(card).getByRole("img", { name: "Strictness 2 of 3" })).toBeInTheDocument();
    expect(screen.queryByTestId("rules-quick-custom")).toBeNull();
  });

  it("picker: says Custom: matches none with the nearest level when nothing matches", async () => {
    vi.mocked(api.ruleProfiles).mockResolvedValue({
      ...profiles,
      profiles: profiles.profiles.map((p) => (p.id === "standard" ? { ...p, pins: [...p.pins.slice(0, 2), pin("review.gap_blocks", "off")] } : p)),
    });
    panel();
    expect(await screen.findByTestId("rules-quick-custom")).toHaveTextContent("Custom: matches none. Nearest is Standard, which differs in 1 rule.");
  });

  it("compare: outlines the cells that would change, marks WEAKENING and the org-locked row", async () => {
    panel({ screen: "compare", profile: "exploratory" });
    expect(await screen.findByTestId("rules-quick-cell-change-review.sabotage")).toBeInTheDocument();
    expect(screen.getByTestId("rules-quick-weakening-review.sabotage")).toHaveTextContent("WEAKENING");
    expect(screen.getByTestId("rules-quick-locked-review.gap_blocks")).toHaveTextContent("set by org acme · locked");
    expect(screen.queryByTestId("rules-quick-cell-change-review.gap_blocks")).toBeNull();
    expect(screen.getByTestId("rules-quick-repo-note")).toHaveTextContent("These depend on your repository");
  });

  it("compare narrows to one card per rule", async () => {
    panel({ screen: "compare", profile: "regulated" }, vi.fn(), true);
    const row = await screen.findByTestId("rules-quick-row-review.sabotage");
    expect(row).toHaveTextContent("Regulated: on · min_tests_failed: 2");
    expect(screen.queryByTestId("rules-quick-table")).toBeNull();
  });

  it("review: one prefixed reason, a dropped rule is sent as an exception, a locked rule that already matches is still skipped", async () => {
    const user = userEvent.setup();
    const onApplied = vi.fn();
    vi.mocked(api.applyRuleProfile).mockResolvedValue({ project_id: "core", profile: "regulated", applied: ["review.sabotage"], excepted: ["review.cross_vendor"], skipped_locked: [] });
    panel({ screen: "compare", profile: "regulated" }, onApplied);
    await user.click(await screen.findByTestId("rules-quick-review"));
    const sheet = screen.getByTestId("rules-review-sheet");
    expect(within(sheet).getByTestId("rules-review-reason-prefix")).toHaveTextContent("applying Regulated 0.1.0:");
    await user.click(within(sheet).getByTestId("rules-review-drop-review.cross_vendor"));
    expect(within(sheet).queryByTestId("rules-review-rule-review.cross_vendor")).toBeNull();
    expect(within(sheet).getByTestId("rules-quick-dropped")).toHaveTextContent("Recorded as Regulated, except: T review.cross_vendor");
    expect(within(sheet).getByTestId("rules-quick-skipped")).toHaveTextContent("Skipped, set by org acme · locked: T review.gap_blocks");
    expect(within(sheet).getByTestId("rules-review-save")).toBeDisabled();
    await user.type(within(sheet).getByLabelText("Reason"), "audit season");
    await user.click(within(sheet).getByTestId("rules-review-save"));
    await waitFor(() => expect(onApplied).toHaveBeenCalledWith(["review.sabotage"]));
    expect(api.applyRuleProfile).toHaveBeenCalledWith("core", "regulated", {
      reason: "applying Regulated 0.1.0: audit season", except: ["review.cross_vendor"], skip_locked: true,
    });
  });

  it("review: a weakening level needs the risk confirmed and sends skip_locked for the locked rule", async () => {
    const user = userEvent.setup();
    vi.mocked(api.applyRuleProfile).mockResolvedValue({ project_id: "core", profile: "exploratory", applied: ["review.sabotage"], excepted: [], skipped_locked: ["review.gap_blocks"] });
    panel({ screen: "compare", profile: "exploratory" });
    await user.click(await screen.findByTestId("rules-quick-review"));
    const sheet = screen.getByTestId("rules-review-sheet");
    expect(within(sheet).getByTestId("rules-quick-skipped")).toHaveTextContent("Skipped, set by org acme · locked: T review.gap_blocks");
    await user.type(within(sheet).getByLabelText("Reason"), "prototype");
    expect(within(sheet).getByTestId("rules-review-save")).toBeDisabled();
    await user.click(within(within(sheet).getByTestId("rules-review-confirm-risk")).getByRole("checkbox"));
    await user.click(within(sheet).getByTestId("rules-review-save"));
    await waitFor(() => expect(api.applyRuleProfile).toHaveBeenCalled());
    expect(vi.mocked(api.applyRuleProfile).mock.calls[0][2]).toMatchObject({ except: [], skip_locked: true });
  });

  it("drift: the chip counts rules changed by hand and the compare view marks them", async () => {
    const user = userEvent.setup();
    vi.mocked(api.ruleProfiles).mockResolvedValue({
      ...profiles,
      applied: { profile: "regulated", version: "0.1.0", pack_version: "0.4.0", exceptions: [], skipped: [], by: "u", at: "" },
    });
    vi.mocked(api.ruleProfileDrift).mockResolvedValue({
      project_id: "core", profile: "regulated", state: "drifted", differs_in: 1, summary: "", weakening: 1,
      drift: [{ rule_id: "review.cross_vendor", kind: "weakening", detail: "pinned on, is off (project)" }],
    });
    const onOpen = vi.fn();
    wrap(<QuickConfigureBar onOpen={onOpen} />);
    const chip = await screen.findByTestId("rules-level-chip");
    await waitFor(() => expect(chip).toHaveTextContent("Level: Regulated v0.1.0 · drifted (1)"));
    await user.click(chip);
    expect(onOpen).toHaveBeenCalledWith({ screen: "compare", profile: "regulated" });
    panel({ screen: "compare", profile: "regulated" });
    expect(await screen.findByTestId("rules-quick-drift-review.cross_vendor")).toHaveTextContent("CHANGED BY HAND · WEAKENING");
  });

  it("pack update: offered on the bar, reviewed on the same sheet, and accepted only on save", async () => {
    const user = userEvent.setup();
    const onApplied = vi.fn();
    vi.mocked(api.ruleProfiles).mockResolvedValue({
      ...profiles,
      profiles: profiles.profiles.map((p) => (p.id === "regulated" ? { ...p, version: "0.2.0" } : p)),
      applied: { profile: "regulated", version: "0.1.0", pack_version: "0.3.0", exceptions: ["review.cross_vendor"], skipped: [], by: "u", at: "" },
    });
    vi.mocked(api.ruleProfileDrift).mockResolvedValue({ project_id: "core", profile: "regulated", state: "clean", differs_in: 0, summary: "", weakening: 0, drift: [] });
    vi.mocked(api.ruleProfileUpdates).mockResolvedValue({
      project_id: "core",
      updates: [{ profile: "regulated", applied_version: "0.1.0", offered_version: "0.2.0", pack_version: "0.4.0", weakening: 0,
        diff: [{ rule_id: "review.sabotage", change: "params_changed", kind: "changed" }, { rule_id: "review.cross_vendor", change: "state_changed", kind: "changed" }] }],
    });
    vi.mocked(api.acceptRuleProfileUpdate).mockResolvedValue({ project_id: "core", profile: "regulated", applied: ["review.sabotage"], excepted: ["review.cross_vendor"], skipped_locked: [] });
    const onOpen = vi.fn();
    wrap(<QuickConfigureBar onOpen={onOpen} />);
    expect(await screen.findByTestId("rules-level-update")).toHaveTextContent("A newer Regulated is available: 2 rules change");
    await user.click(screen.getByTestId("rules-open-update"));
    expect(onOpen).toHaveBeenCalledWith({ screen: "update", profile: "regulated" });

    panel({ screen: "update", profile: "regulated" }, onApplied);
    const sheet = await screen.findByTestId("rules-review-sheet");
    expect(within(sheet).getByTestId("rules-quick-update-note")).toHaveTextContent("Regulated v0.1.0 → v0.2.0. Nothing changes until you accept.");
    expect(within(sheet).getByTestId("rules-quick-carried")).toHaveTextContent("T review.cross_vendor");
    expect(within(sheet).queryByTestId("rules-review-drop-review.sabotage")).toBeNull();
    expect(api.acceptRuleProfileUpdate).not.toHaveBeenCalled();
    await user.type(within(sheet).getByLabelText("Reason"), "new pack");
    await user.click(within(sheet).getByTestId("rules-review-save"));
    await waitFor(() => expect(onApplied).toHaveBeenCalledWith(["review.sabotage"]));
    expect(api.acceptRuleProfileUpdate).toHaveBeenCalledWith("core", "regulated", { reason: "applying Regulated 0.2.0: new pack", version: "0.2.0" });
  });

  it("a refusal stays on the sheet with the server's words and applies nothing", async () => {
    const user = userEvent.setup();
    const onApplied = vi.fn();
    vi.mocked(api.applyRuleProfile).mockRejectedValue(new Error(JSON.stringify({ detail: "profile 'regulated' names rules the org has locked" })));
    panel({ screen: "compare", profile: "regulated" }, onApplied);
    await user.click(await screen.findByTestId("rules-quick-review"));
    await user.type(screen.getByLabelText("Reason"), "x");
    await user.click(screen.getByTestId("rules-review-save"));
    expect(await screen.findByTestId("rules-review-error")).toHaveTextContent("names rules the org has locked");
    expect(onApplied).not.toHaveBeenCalled();
  });
});
