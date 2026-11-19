/**
 * The docked rule editor (GRPH-1718, layout C): a stage opens under the map, the rule list groups
 * by kind, one rule edits at a time and saves through the batch write with its whole params.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DockEditor } from "@/features/rules/DockEditor";
import { RulesView } from "@/features/rules/RulesView";
import { api } from "@/lib/api";
import type { RuleFlowRule, RuleStage, RulesFlow, RuleToggle } from "@/lib/types";

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

const ALL = ["push_to_base", "prs_to_base", "prs_to_integration"];

function rule(over: Partial<RuleFlowRule> & { id: string; stage: string }): RuleFlowRule {
  return {
    title: over.id, summary: `${over.id} summary`, enforcement: "enforced", channel: null,
    enforcer: { kind: "graphban_source", ref: "a.py", url: "" }, truth: "db", scope: "project", models: ALL,
    value: true, source: "platform default", used: true, label: null, skills: null, control: "toggle",
    params: { value: {}, source: "platform default" }, locked: false, ...over,
  } as RuleFlowRule;
}

const builtin = rule({ id: "review.not_own_work", stage: "review", title: "Not your own work", control: null, params: null, truth: "code" });
const sabotage = rule({
  id: "review.sabotage_required", stage: "review", title: "Sabotage required",
  params: { value: { min_effort: 3 }, source: "platform default" },
});
const gitopsBar = rule({ id: "gitops.reviewer_bar", stage: "review", title: "Reviewer bar", control: null, params: null, value: "sign_off", source: "project" });
const reviewStage = { id: "review", title: "Review", summary: "", state: "enforced", rules: [builtin, sabotage, gitopsBar] } as unknown as RuleStage;

const generated = rule({
  id: "build.generated_files", stage: "build", title: "Generated files",
  params: { value: { paths: ["a.lock"] }, source: "project" },
});
const done = rule({
  id: "merge.done_predicates", stage: "merge", title: "Done predicates",
  params: { value: { predicates: ["suite_green"] }, source: "platform default" },
});

function toggle(id: string, over: Partial<RuleToggle> = {}): RuleToggle {
  return {
    rule_id: id, stage: "x", title: id, on: { value: true, source: "platform default" },
    params: { value: {}, source: "platform default" }, locked: false, locked_by: null, ...over,
  } as RuleToggle;
}

function stageOf(id: string, title: string, rules: RuleFlowRule[]): RuleStage {
  return { id, title, summary: "", state: "enforced", rules } as unknown as RuleStage;
}

function qc() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function dock(stage: RuleStage, props: Partial<React.ComponentProps<typeof DockEditor>> = {}) {
  return render(
    <QueryClientProvider client={qc()}>
      <DockEditor stage={stage} version="1.0.0" model="prs_to_base" focusRule={null} prefill={null} onClose={() => {}} {...props} />
    </QueryClientProvider>,
  );
}

const flow: RulesFlow = {
  version: "1.0.0", model: "prs_to_base",
  control: { state: "local", writable: true, message: "", org_name: "" },
  edges: [["build", "merge"]],
  stages: [
    stageOf("build", "Build", [generated]),
    stageOf("merge", "Merge", [done]),
  ],
} as unknown as RulesFlow;

function view(url: string) {
  return render(
    <QueryClientProvider client={qc()}>
      <MemoryRouter initialEntries={[url]}>
        <RulesView />
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

afterEach(() => vi.restoreAllMocks());

describe("docked editor", () => {
  it("stage opens docked under map", async () => {
    view("/rules?stage=build");
    const ins = await screen.findByTestId("rules-inspector");
    const map = screen.getByTestId("rules-map-area");
    // The map stays above, as a strip with the stage highlighted; the dock follows it in the page.
    expect(map.compareDocumentPosition(ins) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(map).getByTestId("rules-stage-build")).toHaveAttribute("aria-selected", "true");
    expect(within(ins).getByTestId("rules-dock-editor")).toBeInTheDocument();
    expect(screen.queryByTestId("rules-stage-strip")).not.toBeInTheDocument();
    expect(map).toHaveAttribute("data-map-pct", "34");
  });

  it("dock height presets persist and survive storage failure", async () => {
    const user = userEvent.setup();
    const first = view("/rules?stage=build");
    await screen.findByTestId("rules-inspector");
    await user.click(screen.getByTestId("rules-dock-height-half"));
    expect(screen.getByTestId("rules-map-area")).toHaveAttribute("data-map-pct", "46");
    expect(screen.getByTestId("rules-map-area")).toHaveStyle({ height: "46%" });
    await user.click(screen.getByTestId("rules-dock-height-full"));
    expect(screen.getByTestId("rules-map-area")).toHaveStyle({ height: "18%" });
    await user.click(screen.getByTestId("rules-dock-height-tall"));
    expect(screen.getByTestId("rules-map-area")).toHaveStyle({ height: "34%" });
    await user.click(screen.getByTestId("rules-dock-height-half"));
    first.unmount();
    // A reload remembers the choice.
    view("/rules?stage=build");
    await screen.findByTestId("rules-inspector");
    expect(screen.getByTestId("rules-map-area")).toHaveAttribute("data-map-pct", "46");
  });

  it("dock height presets persist and survive storage failure: a throwing localStorage still renders TALL", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    const user = userEvent.setup();
    view("/rules?stage=build");
    await screen.findByTestId("rules-inspector");
    expect(screen.getByTestId("rules-map-area")).toHaveAttribute("data-map-pct", "34");
    await user.click(screen.getByTestId("rules-dock-height-full")); // setItem throws: still changes for this visit
    expect(screen.getByTestId("rules-map-area")).toHaveAttribute("data-map-pct", "18");
  });

  it("stage switch keeps dock open", async () => {
    const user = userEvent.setup();
    view("/rules?stage=build");
    const ins = await screen.findByTestId("rules-inspector");
    expect(within(ins).getByTestId("rules-dock-row-build.generated_files")).toBeInTheDocument();
    await user.click(screen.getByTestId("rules-stage-merge"));
    const next = await screen.findByRole("region", { name: "Merge stage inspector" });
    expect(within(next).getByTestId("rules-dock-row-merge.done_predicates")).toBeInTheDocument();
    expect(within(next).queryByTestId("rules-dock-row-build.generated_files")).toBeNull();
    expect(screen.getByTestId("rules-listbox")).toBeInTheDocument();
  });

  it("rule list groups and selects", async () => {
    const user = userEvent.setup();
    dock(reviewStage);
    for (const [g, n] of [["builtin", 1], ["project", 1], ["library", 1]] as const) {
      expect(within(screen.getByTestId(`rules-dock-group-${g}`)).getAllByTestId(/^rules-dock-row-/)).toHaveLength(n);
    }
    expect(screen.getByTestId("rules-dock-group-builtin")).toHaveTextContent("BUILT-IN RULES · 1");
    expect(screen.getByTestId("rules-dock-group-project")).toHaveTextContent("PROJECT SETTINGS · 1");
    expect(screen.getByTestId("rules-dock-group-library")).toHaveTextContent("LIBRARY RULES · 1");
    expect(screen.getByTestId("rules-dock-row-review.not_own_work")).toHaveTextContent("BUILT IN");
    await user.click(screen.getByTestId("rules-dock-row-review.sabotage_required"));
    expect(screen.getByTestId("rules-dock-row-review.sabotage_required")).toHaveAttribute("aria-current", "true");
    expect(within(screen.getByTestId("rules-dock-form")).getByText("Sabotage required")).toBeInTheDocument();
    expect(within(screen.getByTestId("rules-dock-form")).getByTestId("rules-dock-param-min_effort")).toBeInTheDocument();
  });

  it("save needs a reason and sends one batch write with whole params", async () => {
    const user = userEvent.setup();
    vi.mocked(api.setRuleToggles).mockResolvedValue({ toggles: [] });
    const two = rule({
      id: "x.two", stage: "review", title: "Two params",
      params: { value: { min_effort: 3, mode: "strict" }, source: "platform default" },
    });
    dock(stageOf("review", "Review", [two]));
    expect(screen.getByTestId("rules-dock-nochange")).toHaveTextContent(
      "No changes. Edit a value to see the diff and save it with a reason.",
    );
    const save = screen.getByTestId("rules-dock-save-button");
    expect(save).toBeDisabled();
    const effort = await screen.findByLabelText("min_effort");
    await user.clear(effort);
    await user.type(effort, "5");
    expect(screen.getByTestId("rules-dock-diff")).toHaveTextContent("min_effort 3 → 5");
    expect(save).toBeDisabled();
    await user.type(screen.getByLabelText("Reason"), "seven ch");
    expect(save).toBeEnabled(); // exactly 8 characters
    await user.clear(screen.getByLabelText("Reason"));
    await user.type(screen.getByLabelText("Reason"), "short");
    expect(save).toBeDisabled();
    await user.type(screen.getByLabelText("Reason"), " enough");
    await user.click(save);
    await waitFor(() => expect(api.setRuleToggles).toHaveBeenCalledTimes(1));
    const [, body] = vi.mocked(api.setRuleToggles).mock.calls[0];
    expect(body.reason).toBe("short enough");
    expect(body.toggles).toHaveLength(1);
    expect(body.toggles[0].params).toEqual({ min_effort: 5, mode: "strict" }); // the whole object, untouched keys included
    expect(await screen.findByTestId("rules-dock-saved")).toHaveTextContent("✓Saved by you · min_effort 3 → 5 · in force on the next sign-off");
  });

  it("list param with two entries saves both", async () => {
    const user = userEvent.setup();
    vi.mocked(api.setRuleToggles).mockResolvedValue({ toggles: [] });
    dock(stageOf("build", "Build", [generated]));
    await user.click(await screen.findByRole("button", { name: "Add path" }));
    await user.type(screen.getByLabelText("paths 2"), "b.lock");
    await user.type(screen.getByLabelText("Reason"), "ignore both lockfiles");
    await user.click(screen.getByTestId("rules-dock-save-button"));
    await waitFor(() => expect(api.setRuleToggles).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.setRuleToggles).mock.calls[0][1].toggles[0].params).toEqual({ paths: ["a.lock", "b.lock"] });
  });

  it("done predicates save keeps the floor", async () => {
    const user = userEvent.setup();
    vi.mocked(api.setRuleToggles).mockResolvedValue({ toggles: [] });
    vi.mocked(api.ruleToggles).mockResolvedValue({
      library_loaded: true,
      toggles: [toggle("merge.done_predicates", { effective_predicates: [{ name: "suite_green", source: "deployment" }] })],
    });
    dock(stageOf("merge", "Merge", [done]));
    await waitFor(() => expect(screen.getAllByTestId("rules-predicate-floor")).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: /Suggested: add scans_clean/ }));
    await user.type(screen.getByLabelText("Reason"), "scans gate done");
    await user.click(screen.getByTestId("rules-dock-save-button"));
    await waitFor(() => expect(api.setRuleToggles).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.setRuleToggles).mock.calls[0][1].toggles[0].params).toEqual({ predicates: ["suite_green", "scans_clean"] });
  });

  it("discard and dirty-leave confirm", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm");
    dock(reviewStage);
    await user.click(screen.getByTestId("rules-dock-row-review.sabotage_required"));
    const effort = await screen.findByLabelText("min_effort");
    await user.clear(effort);
    await user.type(effort, "9");
    expect(screen.getByTestId("rules-dock-diff")).toBeInTheDocument();
    // Discard restores the saved value.
    await user.click(screen.getByTestId("rules-dock-discard"));
    expect(screen.getByLabelText("min_effort")).toHaveValue("3");
    expect(screen.getByTestId("rules-dock-nochange")).toBeInTheDocument();
    // Dirty again, then leave: an in-page confirm, never window.confirm.
    await user.clear(screen.getByLabelText("min_effort"));
    await user.type(screen.getByLabelText("min_effort"), "9");
    await user.click(screen.getByTestId("rules-dock-row-review.not_own_work"));
    expect(screen.getByTestId("rules-dock-leave")).toHaveTextContent("unsaved changes");
    expect(screen.getByTestId("rules-dock-row-review.sabotage_required")).toHaveAttribute("aria-current", "true");
    await user.click(screen.getByTestId("rules-dock-leave-stay"));
    expect(screen.queryByTestId("rules-dock-leave")).toBeNull();
    await user.click(screen.getByTestId("rules-dock-row-review.not_own_work"));
    await user.click(screen.getByTestId("rules-dock-leave-confirm"));
    expect(screen.getByTestId("rules-dock-row-review.not_own_work")).toHaveAttribute("aria-current", "true");
    expect(confirm).not.toHaveBeenCalled();
  });

  it("resolution highlights the winner", async () => {
    const t = (on: RuleToggle["on"]["source"], params: RuleToggle["params"]["source"]) =>
      toggle("review.sabotage_required", { on: { value: true, source: on }, params: { value: { min_effort: 3 }, source: params } });
    const winner = async () => {
      const chain = await screen.findByTestId("rules-dock-chain");
      return ["project", "org", "platform"].filter((id) => within(chain).getByTestId(`rules-dock-chain-${id}`).getAttribute("aria-current") === "true");
    };
    vi.mocked(api.ruleToggles).mockResolvedValue({ library_loaded: true, toggles: [t("platform default", "project")] });
    const a = dock(stageOf("review", "Review", [sabotage]));
    await waitFor(async () => expect(await winner()).toEqual(["project"]));
    a.unmount();
    vi.mocked(api.ruleToggles).mockResolvedValue({ library_loaded: true, toggles: [t("org", "org")] });
    const b = dock(stageOf("review", "Review", [sabotage]));
    await waitFor(async () => expect(await winner()).toEqual(["org"]));
    b.unmount();
    vi.mocked(api.ruleToggles).mockResolvedValue({ library_loaded: true, toggles: [t("platform default", "platform default")] });
    dock(stageOf("review", "Review", [sabotage]));
    await waitFor(async () => expect(await winner()).toEqual(["platform"]));
  });

  it("effect column renders /rules/effect data", async () => {
    const m = (value: number | null, status: "measured" | "insufficient" = "measured") => ({ status, value, n: 9, min_n: 5, reason: null });
    vi.mocked(api.ruleEffect).mockResolvedValue({
      scope: "project",
      rules: [{ rule_id: "review.sabotage_required", caught: m(4), cost: { overridden: m(1), held_seconds: m(null, "insufficient") }, escape_rate: { before: m(0.2), after: m(0.1) } }],
    } as never);
    dock(stageOf("review", "Review", [sabotage]));
    const fx = await screen.findByTestId("rules-dock-effect");
    await waitFor(() => expect(within(fx).getByTestId("rules-dock-tile-caught-value")).toHaveTextContent(/^4$/));
    expect(within(fx).getByTestId("rules-dock-tile-escapes-value")).toHaveTextContent("0.2 → 0.1");
    expect(within(fx).getByTestId("rules-dock-tile-overridden-value")).toHaveTextContent(/^1$/);
    expect(within(fx).getByTestId("rules-dock-tile-cost-value")).toHaveTextContent("not enough data");
  });

  it("effect unavailable is not zero", async () => {
    dock(stageOf("review", "Review", [sabotage]));
    const fx = await screen.findByTestId("rules-dock-effect");
    await waitFor(() => expect(within(fx).getByTestId("rules-dock-effect-unavailable")).toHaveTextContent("unavailable"));
    expect(fx).not.toHaveTextContent(/caught: 0/);
  });

  it("built-in rule is read-only", async () => {
    dock(reviewStage); // the weakest-first default selects a library rule; open the built-in one
    await userEvent.setup().click(screen.getByTestId("rules-dock-row-review.not_own_work"));
    const form = screen.getByTestId("rules-dock-form");
    const controls = form.querySelectorAll("input, textarea, select, button");
    expect(controls.length).toBeGreaterThan(0);
    for (const c of controls) expect(c).toBeDisabled();
    expect(screen.getByTestId("rules-dock-builtin-note")).toHaveTextContent("built in and can't be changed here");
    expect(screen.queryByTestId("rules-dock-save-button")).toBeNull();
    // Description, resolution, effect and history are still shown.
    expect(form).toHaveTextContent("review.not_own_work summary");
    expect(screen.getByTestId("rules-dock-chain")).toBeInTheDocument();
    expect(screen.getByTestId("rules-dock-effect")).toBeInTheDocument();
    // A built-in rule has no change history, and says why rather than showing nothing.
    expect(screen.getByTestId("rules-dock-history-none")).toHaveTextContent("No history: a built-in rule has no setting");
  });

  it("stage switch with unsaved changes asks first", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm");
    view("/rules?stage=build");
    const ins = await screen.findByTestId("rules-inspector");
    await user.click(await within(ins).findByRole("button", { name: "Add path" }));
    await user.type(screen.getByLabelText("paths 2"), "b.lock");
    expect(screen.getByTestId("rules-dock-diff")).toBeInTheDocument();
    // Another stage on the map strip: the draft holds the switch behind an in-page confirm.
    await user.click(screen.getByTestId("rules-stage-merge"));
    expect(screen.getByTestId("rules-stage-leave")).toHaveTextContent("unsaved changes");
    expect(screen.getByRole("region", { name: "Build stage inspector" })).toBeInTheDocument();
    await user.click(screen.getByTestId("rules-stage-leave-stay"));
    expect(screen.queryByTestId("rules-stage-leave")).toBeNull();
    expect(screen.getByLabelText("paths 2")).toHaveValue("b.lock"); // the draft survives "Keep editing"
    // Closing the dock asks too.
    await user.click(screen.getByTestId("rules-dock-close"));
    expect(screen.getByTestId("rules-stage-leave")).toHaveTextContent("Close the editor");
    await user.click(screen.getByTestId("rules-stage-leave-stay"));
    await user.click(screen.getByTestId("rules-stage-merge"));
    await user.click(screen.getByTestId("rules-stage-leave-confirm"));
    const next = await screen.findByRole("region", { name: "Merge stage inspector" });
    expect(within(next).getByTestId("rules-dock-row-merge.done_predicates")).toBeInTheDocument();
    // Clean now: the next switch goes straight through.
    await user.click(screen.getByTestId("rules-stage-build"));
    expect(await screen.findByRole("region", { name: "Build stage inspector" })).toBeInTheDocument();
    expect(screen.queryByTestId("rules-stage-leave")).toBeNull();
    expect(confirm).not.toHaveBeenCalled();
  });

  it("Recommended header with unsaved dock draft asks first", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm");
    vi.mocked(api.recommendedRules).mockResolvedValue({ sources: [], suggestions: [] });
    view("/rules?stage=build");
    await screen.findByRole("region", { name: "Build stage inspector" });
    await user.click(screen.getByRole("button", { name: "Add path" }));
    await user.type(screen.getByLabelText("paths 2"), "b.lock");
    expect(screen.getByTestId("rules-dock-diff")).toBeInTheDocument();
    await user.click(screen.getByTestId("rules-open-recommended"));
    expect(screen.getByTestId("rules-stage-leave")).toHaveTextContent("unsaved changes");
    expect(screen.getByRole("region", { name: "Build stage inspector" })).toBeInTheDocument();
    await user.click(screen.getByTestId("rules-stage-leave-stay"));
    expect(screen.queryByTestId("rules-stage-leave")).toBeNull();
    expect(screen.getByLabelText("paths 2")).toHaveValue("b.lock");
    await user.click(screen.getByTestId("rules-open-recommended"));
    await user.click(screen.getByTestId("rules-stage-leave-confirm"));
    await screen.findByTestId("rules-recommended");
    expect(confirm).not.toHaveBeenCalled();
  });

  it("a ?rule= focus does not pull later selections back to the focused stage", async () => {
    const user = userEvent.setup();
    vi.mocked(api.recommendedRules).mockResolvedValue({ sources: [], suggestions: [] });
    view("/rules?rule=build.generated_files");
    await screen.findByRole("region", { name: "Build stage inspector" });
    await user.click(screen.getByTestId("rules-stage-merge"));
    expect(await screen.findByRole("region", { name: "Merge stage inspector" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("region", { name: "Build stage inspector" })).toBeNull());
    await user.click(screen.getByTestId("rules-stage-build"));
    await screen.findByRole("region", { name: "Build stage inspector" });
    await user.click(screen.getByTestId("rules-open-recommended"));
    await screen.findByTestId("rules-recommended");
    await waitFor(() => expect(screen.queryByRole("region", { name: "Build stage inspector" })).toBeNull());
    expect(screen.queryByTestId("rules-stage-leave")).toBeNull();
  });

  it("history shows who, when, reason and before → after", async () => {
    vi.mocked(api.events).mockResolvedValue({
      results: [
        {
          id: 7, ts: null, actor_type: "user", actor_id: "u1", actor_label: "Ada", surface: "rest",
          action: "set_rule_toggle", target_type: "rule", target_id: "review.sabotage_required", project_id: "core",
          meta: {
            reason: "raise the bar", effective_before: { on: true, params: { min_effort: 3 } },
            effective_after: { on: false, params: { min_effort: 5 } },
          },
        },
      ],
      total: 1,
    } as never);
    dock(stageOf("review", "Review", [sabotage]));
    const row = await screen.findByTestId("rules-dock-history-7");
    expect(row).toHaveTextContent("Ada");
    expect(row).toHaveTextContent("“raise the bar”");
    expect(row).toHaveTextContent("on/off: ON → OFF");
    expect(row).toHaveTextContent("min_effort: 3 → 5");
  });

  it("each parameter row shows its name and help", async () => {
    dock(stageOf("build", "Build", [generated]));
    const row = await screen.findByTestId("rules-dock-param-paths");
    expect(row).toHaveTextContent("paths");
    expect(within(row).getByTestId("rules-dock-help-paths")).toHaveTextContent("Generated files a builder may change");
  });
});

// ── GRPH-1725: the gaps between GRPH-1718 and the owner's layout C ───────────────────────────

/** A ResizeObserver the test drives: `resize(w, h)` reports a new box to every observer. */
function fakeResizeObserver() {
  const live: { cb: ResizeObserverCallback; el: Element }[] = [];
  class RO {
    cb: ResizeObserverCallback;
    constructor(cb: ResizeObserverCallback) {
      this.cb = cb;
    }
    observe(el: Element) {
      live.push({ cb: this.cb, el });
    }
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", RO);
  return (width: number, height: number) =>
    act(() => {
      for (const o of live) o.cb([{ contentRect: { width, height } } as ResizeObserverEntry], {} as ResizeObserver);
    });
}

const SEVEN = ["plan", "claim", "build", "review", "merge", "release", "learn"];
const sevenFlow = {
  ...flow,
  stages: SEVEN.map((id) => stageOf(id, id[0].toUpperCase() + id.slice(1), [rule({ id: `${id}.r`, stage: id, title: `${id} rule` })])),
  edges: SEVEN.slice(1).map((id, i) => [SEVEN[i], id]),
} as unknown as RulesFlow;

const checkedRule = rule({
  id: "review.ci_attested", stage: "review", title: "CI attested", enforcement: "checked", control: null, params: null,
  truth: "derived", last_result: { outcome: "pass", at: null, item: "GRPH-9" } as never,
});

describe("GRPH-1725 layout C", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("map strip shows the opened stage highlighted and no legend while docked", async () => {
    const user = userEvent.setup();
    view("/rules");
    // Undocked, the map keeps its legend and zoom bar.
    await screen.findByTestId("rules-canvas");
    expect(screen.getByTestId("rules-legend")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Zoom in" })).toBeInTheDocument();
    await user.click(screen.getByTestId("rules-stage-build"));
    await screen.findByTestId("rules-inspector");
    expect(screen.queryByTestId("rules-legend")).toBeNull();
    expect(screen.queryByRole("button", { name: "Zoom in" })).toBeNull();
    expect(screen.queryByTestId("rules-zoom-pct")).toBeNull();
    expect(screen.getByTestId("rules-stage-build")).toHaveAttribute("data-selected", "true");
    expect(screen.getByTestId("rules-stage-merge")).not.toHaveAttribute("data-selected");
  });

  it("map strip fits the stages to its height", async () => {
    const resize = fakeResizeObserver();
    vi.mocked(api.rulesFlow).mockResolvedValue(sevenFlow);
    const user = userEvent.setup();
    view("/rules?stage=build");
    await screen.findByTestId("rules-inspector");
    const canvas = screen.getByTestId("rules-canvas");
    expect(canvas).toHaveAttribute("data-docked", "true");
    // Seven stages are 1696 wide: a 1500-wide strip fits them at (1500 - 48) / 1696.
    resize(1500, 300);
    await waitFor(() => expect(canvas).toHaveAttribute("data-zoom", "0.856"));
    // A short strip (FULL) is height-bound: the card row fits (110 - 16) / 128.
    const before = Number(canvas.getAttribute("data-fits"));
    await user.click(screen.getByTestId("rules-dock-height-full"));
    expect(Number(canvas.getAttribute("data-fits"))).toBeGreaterThan(before); // the preset change refits
    resize(1500, 110);
    await waitFor(() => expect(canvas).toHaveAttribute("data-zoom", "0.734"));
  });

  it("FULL gives the dock the space under the page header", async () => {
    vi.mocked(api.rulesFlow).mockResolvedValue({ ...flow, model: null } as RulesFlow);
    vi.mocked(api.ruleProfiles).mockResolvedValue({
      project_id: "core", pack_loaded: true, pack_version: "0.4.0", order: ["standard"],
      profiles: [{ id: "standard", title: "Standard", summary: "", version: "0.1.0", pins: [] }], applied: null,
    } as never);
    vi.mocked(api.ruleProfileUpdates).mockResolvedValue({ project_id: "core", updates: [] } as never);
    const user = userEvent.setup();
    view("/rules");
    const banner = await screen.findByTestId("rules-fresh-banner");
    expect(banner).not.toHaveAttribute("data-collapsed");
    expect(await screen.findByTestId("rules-quick-bar")).not.toHaveAttribute("data-collapsed");
    await user.click(screen.getByTestId("rules-stage-build"));
    await screen.findByTestId("rules-inspector");
    expect(screen.getByTestId("rules-fresh-banner")).toHaveAttribute("data-collapsed", "true");
    expect(screen.queryByTestId("rules-fresh-setup")).toBeNull(); // one line: the big buttons are gone
    expect(screen.getByTestId("rules-quick-bar")).toHaveAttribute("data-collapsed", "true");
    expect(screen.getByTestId("rules-quick-bar").className).toContain("flex-nowrap");
  });

  it("no duplicate rule card", async () => {
    const user = userEvent.setup();
    dock(stageOf("review", "Review", [builtin, sabotage, checkedRule]));
    await user.click(screen.getByTestId("rules-dock-row-review.sabotage_required"));
    const form = screen.getByTestId("rules-dock-form");
    expect(within(form).getAllByText("Sabotage required")).toHaveLength(1);
    expect(form.querySelectorAll("[data-testid^='rules-row-']")).toHaveLength(1);
    await user.click(screen.getByTestId("rules-dock-row-review.ci_attested"));
    const head = within(form).getByTestId("rules-row-review.ci_attested");
    expect(within(form).getAllByText("CI attested")).toHaveLength(1);
    // Enforcer and last result live in the form header.
    expect(within(head).getByTestId("rules-dock-enforced-by")).toHaveTextContent("Enforced by");
    expect(within(head).getByTestId("rules-enforcer")).toBeInTheDocument();
    expect(within(head).getByTestId("rules-result")).toHaveTextContent("PASS");
    expect(form.querySelectorAll("[data-testid='rules-enforcer']")).toHaveLength(1);
  });

  it("project setting has no on/off control", async () => {
    const user = userEvent.setup();
    dock(reviewStage);
    await user.click(screen.getByTestId("rules-dock-row-gitops.reviewer_bar"));
    expect(screen.getByTestId("rules-dock-row-gitops.reviewer_bar")).toHaveAttribute("aria-current", "true");
    expect(screen.queryByTestId("rules-dock-state")).toBeNull();
    // A library rule keeps it.
    await user.click(screen.getByTestId("rules-dock-row-review.sabotage_required"));
    expect(screen.getByTestId("rules-dock-state")).toBeInTheDocument();
  });

  it("resolution shows the real org and each level's value", async () => {
    vi.mocked(api.syncStatus).mockResolvedValue({ linked: true, org: "Acme" } as never);
    vi.mocked(api.ruleToggles).mockResolvedValue({
      library_loaded: true,
      toggles: [toggle("review.sabotage_required", {
        on: { value: true, source: "project" }, params: { value: { min_tests_failed: 2 }, source: "project" },
        inherits: { on: "platform default", params: "platform default" },
      })],
    });
    const a = dock(stageOf("review", "Review", [sabotage]));
    const chain = await screen.findByTestId("rules-dock-chain");
    await waitFor(() => expect(within(chain).getByTestId("rules-dock-chain-org")).toHaveTextContent("ORG Acme"));
    expect(chain).not.toHaveTextContent("your org");
    expect(within(chain).getAllByText(/PLATFORM DEFAULT/)).toHaveLength(1);
    expect(within(chain).getByTestId("rules-dock-chain-project")).toHaveTextContent("PROJECTon · min_tests_failed 2IN FORCE");
    expect(within(chain).getByTestId("rules-dock-chain-org-value")).toHaveTextContent("not set");
    expect(within(chain).getByTestId("rules-dock-chain-platform-value")).toHaveTextContent("overridden");
    expect(within(chain).getAllByText("IN FORCE")).toHaveLength(1);
    a.unmount();
    // No org: said as none, and the platform default wins with its value.
    vi.mocked(api.syncStatus).mockResolvedValue({ linked: false, org: "" } as never);
    vi.mocked(api.ruleToggles).mockResolvedValue({
      library_loaded: true,
      toggles: [toggle("review.sabotage_required", { on: { value: false, source: "platform default" }, params: { value: { min_effort: 3 }, source: "platform default" } })],
    });
    dock(stageOf("review", "Review", [sabotage]));
    const c2 = await screen.findByTestId("rules-dock-chain");
    await waitFor(() => expect(within(c2).getByTestId("rules-dock-chain-platform")).toHaveTextContent("PLATFORM DEFAULToff · min_effort 3IN FORCE"));
    expect(within(c2).getByTestId("rules-dock-chain-org")).toHaveTextContent(/^ORG · none$/);
    expect(within(c2).getByTestId("rules-dock-chain-project-value")).toHaveTextContent("not set");
  });

  it("param name rendered once", async () => {
    dock(stageOf("review", "Review", [sabotage]));
    const row = await screen.findByTestId("rules-dock-param-min_effort");
    expect(within(row).getAllByText("min_effort")).toHaveLength(1);
    // The editor still names its input for assistive tech.
    expect(within(row).getByLabelText("min_effort")).toBeInTheDocument();
  });

  it("header shows level pill, kind · stage, state badge, enforcer and state note", async () => {
    const user = userEvent.setup();
    vi.mocked(api.ruleToggles).mockResolvedValue({
      library_loaded: true,
      toggles: [toggle("review.sabotage_required", { on: { value: true, source: "project" }, params: { value: { min_effort: 3 }, source: "platform default" } })],
    });
    dock(reviewStage);
    await user.click(screen.getByTestId("rules-dock-row-review.sabotage_required"));
    const head = screen.getByTestId("rules-row-review.sabotage_required");
    expect(within(head).getByTestId("rules-dock-level-pill")).toHaveTextContent("ENFORCED");
    expect(within(head).getByTestId("rules-dock-level-pill").querySelector("svg")).not.toBeNull();
    expect(within(head).getByTestId("rules-dock-kind")).toHaveTextContent("LIBRARY RULE · REVIEW");
    expect(within(head).getByTestId("rules-dock-state-badge")).toHaveTextContent(/^ON$/);
    expect(within(head).getByTestId("rules-dock-enforced-by")).toHaveTextContent("Enforced by");
    await waitFor(() => expect(screen.getByTestId("rules-dock-state-note")).toHaveTextContent("This project switched it on."));
    await user.click(screen.getByTestId("rules-dock-state-off"));
    expect(screen.getByTestId("rules-dock-state-note")).toHaveTextContent("Off for this project, whatever the org sets.");
    await user.click(screen.getByTestId("rules-dock-row-review.not_own_work"));
    await user.click(screen.getByTestId("rules-dock-leave-confirm"));
    expect(screen.getByTestId("rules-dock-kind")).toHaveTextContent("BUILT-IN RULE · REVIEW");
    await user.click(screen.getByTestId("rules-dock-row-gitops.reviewer_bar"));
    expect(screen.getByTestId("rules-dock-kind")).toHaveTextContent("PROJECT SETTING · REVIEW");
  });

  it("header shows level pill, kind · stage, state badge, enforcer and state note: inherited off, and the dock header names the rule", async () => {
    const off = rule({ id: "review.sabotage_required", stage: "review", title: "Sabotage required", value: false, used: false, params: { value: { min_effort: 3 }, source: "platform default" } });
    const a = dock(stageOf("review", "Review", [off]));
    expect(screen.getByTestId("rules-dock-state-badge")).toHaveTextContent("NOT USED · INHERITED");
    expect(screen.getByTestId("rules-dock-state-note")).toHaveTextContent("Follows the org, then the platform default (off).");
    a.unmount();
    view("/rules?stage=build");
    expect(await screen.findByTestId("rules-dock-rule-title")).toHaveTextContent("Generated files");
  });

  it("list rows show level icon and rule id", () => {
    dock(reviewStage);
    for (const id of ["review.not_own_work", "review.sabotage_required", "gitops.reviewer_bar"]) {
      expect(screen.getByTestId(`rules-dock-rowicon-${id}`).querySelector("svg")).not.toBeNull();
      expect(screen.getByTestId(`rules-dock-rowid-${id}`)).toHaveTextContent(id);
    }
    // Only ON is accent-coloured; BUILT IN and NOT USED are muted.
    expect(screen.getByTestId("rules-dock-rowstate-review.sabotage_required")).toHaveAttribute("data-lit", "true");
    expect(screen.getByTestId("rules-dock-rowstate-review.not_own_work")).not.toHaveAttribute("data-lit");
  });

  it("params table has NAME / VALUE / SET BY columns and a compact stepper", async () => {
    const user = userEvent.setup();
    dock(stageOf("review", "Review", [sabotage, generated]));
    const table = screen.getByTestId("rules-dock-params");
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["NAME", "VALUE", "SET BY"]);
    const value = within(table).getByTestId("rules-dock-value-min_effort");
    const input = within(value).getByLabelText("min_effort");
    expect(input.className).toContain("w-14"); // a narrow stepper, not a full-width input
    expect(within(value).getByLabelText("Increase min_effort")).toBeInTheDocument();
    expect(within(table).getByTestId("rules-dock-source-min_effort")).toHaveTextContent("PLATFORM DEFAULT");
    expect(screen.getByTestId("rules-dock-params-note")).toHaveTextContent("Only the template's own parameters can be set. The server refuses any other.");
    await user.click(within(value).getByLabelText("Increase min_effort"));
    const row = within(table).getByTestId("rules-dock-param-min_effort");
    expect(row).toHaveAttribute("data-changed", "true");
    expect(row).toHaveTextContent("was 3");
    // A list keeps a full-width editor, on its own row under the name.
    await user.click(screen.getByTestId("rules-dock-discard"));
    await user.click(screen.getByTestId("rules-dock-row-build.generated_files"));
    expect(within(screen.getByTestId("rules-dock-params")).getByTestId("rules-dock-param-wide-paths")).toBeInTheDocument();
  });

  it("effect tiles format cost as duration and state the reason once", async () => {
    const m = (value: number | null, n = 9) => ({ status: "measured" as const, value, n, min_n: 5, reason: null });
    const gone = { status: "unavailable" as const, value: null, n: 0, min_n: 5, reason: "the rule has not been adopted in this scope" };
    vi.mocked(api.ruleEffect).mockResolvedValue({
      scope: "project",
      rules: [{
        rule_id: "review.sabotage_required", adopted_at: "2026-10-22T12:00:00Z", escape_note: "observational: no cause is claimed",
        caught: { ...m(2), items: ["GRPH-1", "GRPH-2"] }, cost: { overridden: m(0), held_seconds: m(6480, 3) },
        escape_rate: { before: gone, after: gone },
      }],
    } as never);
    dock(stageOf("review", "Review", [sabotage]));
    const fx = await screen.findByTestId("rules-dock-effect");
    await waitFor(() => expect(within(fx).getByTestId("rules-dock-tile-cost-value")).toHaveTextContent("1h 48m held · 3 items"));
    expect(within(fx).getByTestId("rules-dock-effect-heading")).toHaveTextContent("MEASURED EFFECT · ADOPTED OCT 22");
    expect(within(fx).getByTestId("rules-dock-tile-caught-sub")).toHaveTextContent("GRPH-1, GRPH-2");
    const esc = within(fx).getByTestId("rules-dock-tile-escapes");
    expect(within(esc).getByTestId("rules-dock-tile-escapes-value")).toHaveTextContent(/^unavailable$/);
    expect(esc.textContent!.split("has not been adopted")).toHaveLength(2); // the reason once
    expect(fx).not.toHaveTextContent("→ unavailable");
    expect(within(fx).getByTestId("rules-dock-effect-window")).toHaveTextContent("observational: no cause is claimed");
  });

  it("diff chips, Save N changes, and Cmd+Enter saves", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    vi.mocked(api.setRuleToggles).mockResolvedValue({ toggles: [] });
    dock(stageOf("review", "Review", [sabotage]), { onClose });
    expect(screen.getByTestId("rules-dock-save-button")).toHaveTextContent(/^Save$/);
    const effort = await screen.findByLabelText("min_effort");
    await user.clear(effort);
    await user.type(effort, "5");
    const chip = screen.getByTestId("rules-dock-diff-min_effort");
    expect(chip.querySelector("s")).toHaveTextContent("3");
    expect(chip).toHaveTextContent("min_effort 3 → 5");
    expect(screen.getByTestId("rules-dock-save-button")).toHaveTextContent("Save 1 change");
    await user.click(screen.getByTestId("rules-dock-state-off"));
    expect(screen.getByTestId("rules-dock-save-button")).toHaveTextContent("Save 2 changes");
    const reason = screen.getByLabelText("Reason");
    expect(reason).toHaveAttribute("placeholder", "Why? One sentence, saved as the audit record (required)");
    // The reason sits in the save row with Save and Discard.
    expect(within(screen.getByTestId("rules-dock-save-row")).getByLabelText("Reason")).toBe(reason);
    // Save is disabled without a reason: Cmd+Enter does nothing.
    await user.click(reason);
    await user.keyboard("{Meta>}{Enter}{/Meta}");
    expect(api.setRuleToggles).not.toHaveBeenCalled();
    await user.type(reason, "raise the floor");
    await user.keyboard("{Meta>}{Enter}{/Meta}");
    await waitFor(() => expect(api.setRuleToggles).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId("rules-dock-saved")).toHaveTextContent("✓Saved by you · on/off ON → OFF, min_effort 3 → 5 · in force on the next sign-off");
    // Ctrl+Enter is the same key on other platforms.
    await user.clear(screen.getByLabelText("min_effort"));
    await user.type(screen.getByLabelText("min_effort"), "7");
    await user.type(screen.getByLabelText("Reason"), "and again please");
    await user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(api.setRuleToggles).toHaveBeenCalledTimes(2));
    // Esc still closes the dock.
    await user.click(screen.getByTestId("rules-dock-discard"));
    screen.getByTestId("rules-dock-row-review.sabotage_required").focus();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });
});
