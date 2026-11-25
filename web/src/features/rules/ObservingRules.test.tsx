/** Observing rules, would-have record, promote, nudge, guidance (GRPH-1782). */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { RuleGuidance, RuleProposal, RuleToggle } from "@/lib/types";

import { ObservingRules } from "./ObservingRules";

vi.mock("@/lib/api", () => ({
  api: {
    ruleToggles: vi.fn(), customRules: vi.fn(), ruleGuidance: vi.fn(), ruleObservations: vi.fn(),
    ruleProposals: vi.fn(), promoteRule: vi.fn(), removeCustomRule: vi.fn(), events: vi.fn(),
  },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core" }, projects: [], setActiveId: () => {} }),
}));

function toggle(over: Partial<RuleToggle>): RuleToggle {
  return {
    rule_id: "merge.x", stage: "merge", title: "No big merges", on: { value: true, source: "project" },
    params: { value: {}, source: "project" }, locked: false, locked_by: null, state: "observing", ...over,
  } as RuleToggle;
}
const obs = (over = {}) => ({
  since: "2026-11-01T00:00:00Z", would_hold: 3, would_refuse: 0, would_have: 3,
  decision_due: false, decision_due_reason: null, ...over,
});
const guide: RuleGuidance = {
  id: 7, project_id: "core", kind: "guidance", stage: "Merge", label: "Squash", text: "Squash before merge",
  citations: [{ kind: "item", ref: "GRPH-5" }], proposer: "alex", proposer_kind: "person", status: "adopted",
  created_at: "2026-01-01T00:00:00Z",
};

function view() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ObservingRules stageId="merge" ruleIds={[]} />
    </QueryClientProvider>,
  );
}
function toggles(t: RuleToggle[]) {
  vi.mocked(api.ruleToggles).mockResolvedValue({ library_loaded: true, toggles: t });
}

/** As the list route returns it: no decided_by, decided_reason, or decided_at. */
function adopted(over: Partial<RuleProposal> = {}): RuleProposal {
  return {
    id: 9, project_id: "core", kind: "new_rule", rule_id: "merge.x", primitive: "p", draft: {}, params: {},
    citations: [], proposer: "agent-7", proposer_kind: "agent", direction: "tighten", loosens: false,
    status: "adopted", folded: false, created_at: "2026-01-01T00:00:00Z", ...over,
  };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.customRules).mockResolvedValue([]);
  vi.mocked(api.ruleGuidance).mockResolvedValue([]);
  vi.mocked(api.ruleObservations).mockResolvedValue([]);
  vi.mocked(api.ruleProposals).mockResolvedValue([]);
  vi.mocked(api.events).mockResolvedValue({ results: [], total: 0 } as never);
});

describe("ObservingRules", () => {
  it("draws an observing rule ADVISORY with since date and would-have count", async () => {
    toggles([toggle({ observation: obs() })]);
    view();
    expect(await screen.findByTestId("advisory-badge")).toHaveTextContent("ADVISORY");
    expect(screen.getByTestId("observing-label")).toHaveTextContent("observing since 2026-11-01: would have held 3 items");
    expect(screen.queryByTestId("decision-due")).toBeNull();
  });

  it("reads UNMEASURED, not 0, when nothing was measured; a real 0 stays 0", async () => {
    toggles([toggle({ observation: obs({ would_hold: null, would_have: null }) }), toggle({ rule_id: "merge.y", title: "Y", observation: obs({ would_hold: 0, would_have: 0 }) })]);
    view();
    expect(await screen.findByTestId("observing-merge.x")).toHaveTextContent("UNMEASURED");
    expect(screen.getByTestId("observing-merge.x")).not.toHaveTextContent("would have held 0");
    expect(screen.getByTestId("observing-merge.y")).toHaveTextContent("would have held 0 items");
  });

  it("names who measured: server checked count, supervisor report, not-reported dashed, brief-only", async () => {
    const base = { since: "2026-11-12T00:00:00Z", would_hold: 0, would_refuse: 0, would_allow: 0, would_have: 0, decision_due: false, decision_due_reason: null };
    toggles([
      toggle({ rule_id: "a.server", title: "A", observation: obs({ ...base, measured_by: "server", checks: 38, checks_noun: "sign-offs" }) }),
      toggle({ rule_id: "b.sup", title: "B", observation: obs({ ...base, measured_by: "supervisor", would_allow: 2, would_have: 2, last_supervisor_report: "2026-11-15T00:00:00Z" }) }),
      toggle({ rule_id: "c.none", title: "C", observation: obs({ ...base, measured_by: "supervisor", would_have: null, would_hold: null, last_supervisor_report: null }) }),
      toggle({ rule_id: "d.brief", title: "D", observation: obs({ ...base, measured_by: "brief", would_have: null, would_hold: null }) }),
    ]);
    view();
    expect(await screen.findByTestId("observing-a.server")).toHaveTextContent("Would have refused 0 · 38 sign-offs checked since 12 Nov");
    expect(screen.getByTestId("observing-b.sup")).toHaveTextContent("Would have let through 2 files · reported by the fleet supervisor");
    const none = screen.getByTestId("observing-c.none").querySelector("[data-testid=observing-label]")!;
    expect(none).toHaveTextContent("Would-have not reported yet · no fleet supervisor has run since 12 Nov");
    expect(none.textContent).not.toMatch(/\b0\b/);
    expect(none.getAttribute("data-measure")).toBe("dashed");
    expect(screen.getByTestId("observing-d.brief")).toHaveTextContent("This rule is applied by the agent's brief, which records nothing.");
  });

  it("server rule: verb from the outcomes, checked count only where counted, no-checks is not 0", async () => {
    const base = { since: "2026-11-12T00:00:00Z", would_hold: 0, would_refuse: 0, would_allow: 0, would_have: 0, decision_due: false, decision_due_reason: null };
    toggles([
      // merge.exclusive_areas: held at claim, its claims are not counted -> the plain held count.
      toggle({ rule_id: "e.claim", title: "E", observation: obs({ ...base, measured_by: "server", would_hold: 5, would_have: 5, checks: null, checks_noun: null }) }),
      // a sign-off rule with outcomes recorded and no done item yet: the real count still shows.
      toggle({ rule_id: "f.held", title: "F", observation: obs({ ...base, measured_by: "server", would_hold: 2, would_have: 2, checks: 0, checks_noun: "sign-offs" }) }),
      toggle({ rule_id: "g.none", title: "G", observation: obs({ ...base, measured_by: "server", checks: 0, checks_noun: "sign-offs" }) }),
    ]);
    view();
    const e = (await screen.findByTestId("observing-e.claim")).querySelector("[data-testid=observing-label]")!;
    expect(e).toHaveTextContent("would have held 5 items");
    expect(e.textContent).not.toMatch(/refused|sign-offs/);
    expect(e.getAttribute("data-measure")).toBe("plain");
    expect(screen.getByTestId("observing-f.held")).toHaveTextContent("Would have held 2 · 0 sign-offs checked since 12 Nov");
    const g = screen.getByTestId("observing-g.none").querySelector("[data-testid=observing-label]")!;
    expect(g).toHaveTextContent("Would-have not measured yet · no sign-offs checked since 12 Nov");
    expect(g.textContent).not.toMatch(/\b0\b/);
    expect(g.getAttribute("data-measure")).toBe("dashed");
  });

  it("promote needs a reason and sends it", async () => {
    toggles([toggle({ observation: obs() })]);
    vi.mocked(api.promoteRule).mockResolvedValue(toggle({ state: "active" }));
    view();
    const btn = await screen.findByRole("button", { name: "Promote" });
    expect(btn).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Promote reason for merge.x"), "held 3, all right");
    await userEvent.click(btn);
    expect(api.promoteRule).toHaveBeenCalledWith("core", "merge.x", "held 3, all right");
  });

  it("offers Remove beside Promote for the project's own observing custom rule (GRPH-1939)", async () => {
    const id = "custom.nudge-docs-exclusive";
    toggles([toggle({ rule_id: id, observation: obs({ decision_due: true, decision_due_reason: "10 would-have outcomes" }) })]);
    vi.mocked(api.customRules).mockResolvedValue([
      { rule_id: id, stage: "merge", title: "Docs", state: "observing", source: "custom" },
    ] as never);
    vi.mocked(api.removeCustomRule).mockResolvedValue({ rule_id: id, removed: true } as never);
    view();
    const btn = await screen.findByTestId(`remove-${id}`);
    expect(screen.getByRole("button", { name: "Promote" })).toBeInTheDocument();
    expect(btn).toBeDisabled();
    await userEvent.type(screen.getByLabelText(`Promote reason for ${id}`), "walk is over");
    await userEvent.click(btn);
    expect(api.removeCustomRule).toHaveBeenCalledWith("core", id, "walk is over");
    expect(api.promoteRule).not.toHaveBeenCalled();
  });

  it("offers no Remove for a library rule or an org-owned custom rule", async () => {
    toggles([toggle({ observation: obs() }), toggle({ rule_id: "custom.org-rule", title: "Org", observation: obs() })]);
    vi.mocked(api.customRules).mockResolvedValue([
      { rule_id: "custom.org-rule", stage: "merge", title: "Org", state: "observing", source: "org" },
    ] as never);
    view();
    await screen.findByTestId("observing-custom.org-rule");
    await waitFor(() => expect(api.customRules).toHaveBeenCalled());
    expect(screen.queryByTestId("remove-merge.x")).toBeNull();
    expect(screen.queryByTestId("remove-custom.org-rule")).toBeNull();
  });

  it("names the refusal when a remove is refused", async () => {
    const id = "custom.nudge-docs-exclusive";
    toggles([toggle({ rule_id: id, observation: obs() })]);
    vi.mocked(api.customRules).mockResolvedValue([
      { rule_id: id, stage: "merge", title: "Docs", state: "observing", source: "custom" },
    ] as never);
    vi.mocked(api.removeCustomRule).mockRejectedValue(new Error("only a person sets a rule toggle"));
    view();
    const btn = await screen.findByTestId(`remove-${id}`);
    await userEvent.type(screen.getByLabelText(`Promote reason for ${id}`), "x");
    await userEvent.click(btn);
    expect(await screen.findByTestId(`promote-error-${id}`)).toHaveTextContent(/refused|person/i);
  });

  it("shows the nudge when decision_due, and the would-have record", async () => {
    toggles([toggle({ observation: obs({ decision_due: true, decision_due_reason: "3 would-have outcomes" }) })]);
    vi.mocked(api.ruleObservations).mockResolvedValue([
      { id: 1, rule_id: "merge.x", outcome: "would_hold", item_id: "GRPH-9", commit: null, detail: {}, at: "2026-11-02T00:00:00Z" },
    ]);
    view();
    expect(await screen.findByTestId("decision-due")).toHaveTextContent("promote this rule or remove it");
    await userEvent.click(await screen.findByRole("button", { name: /Would-have record \(1\)/ }));
    expect(screen.getByTestId("would-have-merge.x")).toHaveTextContent("Would have held GRPH-9");
  });

  it("names the adopter and reason from the adopt event, not fields the list omits", async () => {
    // Reading origin.decided_by draws neither: the fixture sends no decided_* fields.
    toggles([toggle({ observation: obs() })]);
    vi.mocked(api.ruleProposals).mockImplementation(async (_p, status) =>
      status === "adopted" ? [adopted()] : [],
    );
    vi.mocked(api.events).mockImplementation(async (q) => ({
      results: q?.action === "adopt_rule_proposal"
        ? [{
            id: 1, ts: "2026-01-02T00:00:00Z", actor_type: "user", actor_id: "u1", actor_label: "Alex",
            surface: "rest", action: "adopt_rule_proposal", target_type: "rule", target_id: "merge.x",
            project_id: "core", meta: { proposal_id: 9, reason: "held three, all right", adopted_by: "Alex" },
          }]
        : [],
      total: 1,
    }) as never);
    view();
    const line = await screen.findByTestId("observing-origin");
    await vi.waitFor(() => expect(line).toHaveTextContent("adopted by Alex"));
    expect(line).toHaveTextContent("Proposed by agent-7");
    expect(line).toHaveTextContent("held three, all right");
    expect(api.events).toHaveBeenCalledWith(expect.objectContaining({ action: "adopt_rule_proposal", projectId: "core" }));
  });

  it("renders adopted guidance as ADVISORY with its citation", async () => {
    toggles([]);
    vi.mocked(api.ruleGuidance).mockResolvedValue([guide]);
    view();
    const g = await screen.findByTestId("guidance-7");
    expect(g).toHaveTextContent("ADVISORY");
    expect(g).toHaveTextContent("Squash before merge");
    expect(g).toHaveTextContent("item GRPH-5");
  });
});
