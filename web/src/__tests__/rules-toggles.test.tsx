/**
 * Library rule toggles on the Rules inspector (GRPH-1268 / PRD-P63 §8, S2c): on, off as NOT
 * USED, locked by the org, a refused template as UNMEASURED, and the org route's 402.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RulesView } from "@/features/rules/RulesView";
import { api } from "@/lib/api";
import type { Event, GitopsView, RuleFlowRule, RulesFlow, RuleToggles } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  api: {
    rulesFlow: vi.fn(), gitops: vi.fn(), config: vi.fn(), syncStatus: vi.fn(), events: vi.fn(),
    ruleToggles: vi.fn(), setRuleToggle: vi.fn(), setOrgRuleToggle: vi.fn(),
  },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core", tag: "CORE", name: "Core" }, projects: [], setActiveId: () => {} }),
}));

const ALL = ["push_to_base", "prs_to_base", "prs_to_integration"];

function lib(id: string, over: Partial<RuleFlowRule> = {}): RuleFlowRule {
  return {
    id, stage: "review", title: id, summary: `${id} summary`, enforcement: "enforced", channel: null,
    enforcer: { kind: "graphban_source", ref: "app/services/fleet.py", url: "" },
    truth: "db", scope: "project", models: ALL, control: "toggle",
    value: true, source: "project", params: { value: { min_effort: 3 }, source: "platform default" },
    locked: false, used: true, label: null, skills: null, ...over,
  };
}

function flow(rules: RuleFlowRule[]): RulesFlow {
  return {
    version: "1.0.0", model: "prs_to_base",
    control: { state: "local", writable: true, message: "", org_name: "" },
    edges: [],
    stages: [{ id: "review", title: "Review", summary: "Review summary", state: "enforced", rules }],
  };
}

function gitops(org_id: string | null = null): GitopsView {
  const f = <T,>(value: T) => ({ value, source: "project" as const });
  return {
    project_id: "core", org_id,
    fields: { base_branch: f("main"), no_push_to_base: f(null), branch_name_pattern: f(null), pr_title_pattern: f(null), reviewer_bar: f(null) },
    control: { state: "local", writable: true, message: "", }, was: null,
    version_from: f(null), release_defined_in: f(null), model: f(null), plan: null, projects: [],
  };
}

/** What `request()` throws: the raw response body as the message. */
function apiError(status: number, body: unknown) {
  return Object.assign(new Error(JSON.stringify(body)), { status });
}

const page = (results: Partial<Event>[]) => ({ results, total: results.length }) as never;

async function openReview(rules: RuleFlowRule[]) {
  vi.mocked(api.rulesFlow).mockResolvedValue(flow(rules));
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/rules?stage=review"]}>
        <RulesView />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return screen.findByTestId("rules-inspector");
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.gitops).mockResolvedValue(gitops());
  vi.mocked(api.config).mockResolvedValue({ hosted_mode: false } as never);
  vi.mocked(api.syncStatus).mockResolvedValue({ linked: false, org: "" } as never);
  vi.mocked(api.ruleToggles).mockResolvedValue({ library_loaded: true, toggles: [] } satisfies RuleToggles);
  vi.mocked(api.events).mockResolvedValue(page([]));
});

describe("library rule toggles", () => {
  it("an on rule shows its switch, source, params and the primitive's level; no bulk enable", async () => {
    const ins = await openReview([lib("review.sabotage_required")]);
    const row = within(ins).getByTestId("rules-row-review.sabotage_required");
    expect(row).toHaveAttribute("data-level", "enforced");
    expect(row).toHaveTextContent("ENFORCED");
    expect(within(row).getByRole("switch")).toHaveAttribute("aria-checked", "true");
    expect(within(row).getByTestId("rules-toggle-source")).toHaveTextContent("PROJECT");
    expect(within(row).getByTestId("rules-toggle-params-source")).toHaveTextContent("PLATFORM DEFAULT");
    expect(within(row).getByLabelText("min_effort")).toHaveValue("3");
    expect(screen.queryByRole("button", { name: /enable all/i })).toBeNull();
  });

  it("an off rule stays listed, drawn NOT USED, dimmed and labelled", async () => {
    const off = lib("review.sabotage_required", { value: false, used: false, label: "turned off for this project" });
    const ins = await openReview([lib("review.other"), off]);
    const row = within(ins).getByTestId("rules-row-review.sabotage_required");
    expect(row).toHaveAttribute("data-level", "not_used");
    expect(row).toHaveTextContent("NOT USED");
    expect(within(row).getByTestId("rules-row-not-used")).toHaveTextContent("turned off for this project");
    expect(within(row).getByRole("switch")).toHaveAttribute("aria-checked", "false");
  });

  it("a change asks for a reason, then the row says who changed it and when", async () => {
    const user = userEvent.setup();
    vi.mocked(api.setRuleToggle).mockResolvedValue({} as never);
    const ins = await openReview([lib("review.sabotage_required", { value: false, used: false })]);
    await user.click(within(ins).getByRole("switch"));
    expect(api.setRuleToggle).not.toHaveBeenCalled();
    const submit = within(ins).getByRole("button", { name: "Turn on" });
    expect(submit).toBeDisabled();
    vi.mocked(api.events).mockResolvedValue(
      page([{ id: 7, ts: new Date().toISOString(), actor_label: "Alex", actor_id: "u1", meta: { reason: "we ship sabotage" } }]),
    );
    await user.type(within(ins).getByLabelText("Reason"), "we ship sabotage");
    await user.click(submit);
    await waitFor(() =>
      expect(api.setRuleToggle).toHaveBeenCalledWith("core", "review.sabotage_required", { on: true, reason: "we ship sabotage" }),
    );
    const changed = await within(ins).findByTestId("rules-toggle-changed");
    expect(changed).toHaveTextContent("Changed by Alex");
    expect(changed).toHaveTextContent("we ship sabotage");
    expect(api.events).toHaveBeenCalledWith(
      expect.objectContaining({ projectId: "core", action: "set_rule_toggle", targetType: "rule", targetId: "review.sabotage_required" }),
    );
  });

  it("params are edited inline and written with a reason", async () => {
    const user = userEvent.setup();
    vi.mocked(api.setRuleToggle).mockResolvedValue({} as never);
    const ins = await openReview([lib("review.sabotage_required")]);
    const input = within(ins).getByLabelText("min_effort");
    await user.clear(input);
    await user.type(input, "5");
    await user.click(within(ins).getByRole("button", { name: "Change params" }));
    await user.type(within(ins).getByLabelText("Reason"), "bigger items only");
    await user.click(within(ins).getByRole("button", { name: "Change params" }));
    await waitFor(() =>
      expect(api.setRuleToggle).toHaveBeenCalledWith("core", "review.sabotage_required", { params: { min_effort: 5 }, reason: "bigger items only" }),
    );
  });

  it("a rule locked by the org has no toggle and says who set it", async () => {
    vi.mocked(api.ruleToggles).mockResolvedValue({
      library_loaded: true,
      toggles: [{ rule_id: "review.sabotage_required", stage: "review", title: "t", on: { value: true, source: "org" },
        params: { value: { min_effort: 3 }, source: "org" }, locked: true, locked_by: "acme" }],
    });
    const ins = await openReview([lib("review.sabotage_required", { locked: true, source: "org" })]);
    const row = within(ins).getByTestId("rules-row-review.sabotage_required");
    expect(await within(row).findByTestId("rules-toggle-locked")).toHaveTextContent("set by org acme · locked");
    expect(within(row).queryByRole("switch")).toBeNull();
    expect(within(row).queryByLabelText("min_effort")).toBeNull();
    expect(within(row).getByTestId("rules-toggle-source")).toHaveTextContent("ORG");
  });

  it("a change the org's lock refuses shows the server's refusal and its see rule link", async () => {
    const user = userEvent.setup();
    vi.mocked(api.setRuleToggle).mockRejectedValue(apiError(409, {
      detail: "review.sabotage_required is locked by the org; a project cannot override it",
      rule_id: "rules.org_lock_holds",
    }));
    const ins = await openReview([lib("review.sabotage_required")]);
    await user.click(within(ins).getByRole("switch"));
    await user.type(within(ins).getByLabelText("Reason"), "try it");
    await user.click(within(ins).getByRole("button", { name: "Turn off" }));
    const alert = await within(ins).findByTestId("rules-toggle-error");
    expect(alert).toHaveTextContent("is locked by the org; a project cannot override it");
    expect(within(alert).getByRole("link", { name: "see rule" })).toHaveAttribute("href", "/rules?rule=rules.org_lock_holds");
  });

  it("a template the loader refused is UNMEASURED with its reason and no toggle", async () => {
    const refused = lib("review.sabotage_required", {
      enforcement: "unmeasured", value: false, used: false, params: { value: {}, source: "platform default" },
      unmeasured_reason: "primitive API major mismatch: template wants 0, server has 1",
    });
    const ins = await openReview([refused]);
    const row = within(ins).getByTestId("rules-row-review.sabotage_required");
    expect(row).toHaveAttribute("data-level", "unmeasured");
    expect(row).toHaveTextContent("UNMEASURED");
    expect(within(row).getByTestId("rules-toggle-refused")).toHaveTextContent("major mismatch");
    expect(within(row).queryByRole("switch")).toBeNull();
    expect(within(row).queryByTestId("rules-row-not-used")).toBeNull();
  });

  it("an org-level change on free/pro shows the API's 402 message", async () => {
    const user = userEvent.setup();
    vi.mocked(api.gitops).mockResolvedValue(gitops("acme"));
    vi.mocked(api.setOrgRuleToggle).mockRejectedValue(apiError(402, {
      detail: "org-level rules need one of: team, enterprise; this org is on 'free' — upgrade the org's plan, or set the rule per project, which every plan can",
    }));
    const ins = await openReview([lib("review.sabotage_required")]);
    await user.click(await within(ins).findByTestId("rules-toggle-org-open"));
    await user.click(within(ins).getByRole("button", { name: "Lock" }));
    await user.type(within(ins).getByLabelText("Reason"), "every project");
    await user.click(within(ins).getByRole("button", { name: "Lock for acme" }));
    await waitFor(() =>
      expect(api.setOrgRuleToggle).toHaveBeenCalledWith("acme", "review.sabotage_required", { locked: true, reason: "every project" }),
    );
    const alert = await within(ins).findByTestId("rules-toggle-error");
    expect(alert).toHaveTextContent("org-level rules need one of: team, enterprise; this org is on 'free'");
    expect(within(alert).queryByRole("link", { name: "see rule" })).toBeNull();
  });
});
