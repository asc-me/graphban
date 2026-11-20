/** Observing rules, would-have record, promote, nudge, guidance (GRPH-1782). */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { RuleGuidance, RuleToggle } from "@/lib/types";

import { ObservingRules } from "./ObservingRules";

vi.mock("@/lib/api", () => ({
  api: {
    ruleToggles: vi.fn(), customRules: vi.fn(), ruleGuidance: vi.fn(), ruleObservations: vi.fn(),
    ruleProposals: vi.fn(), promoteRule: vi.fn(),
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

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.customRules).mockResolvedValue([]);
  vi.mocked(api.ruleGuidance).mockResolvedValue([]);
  vi.mocked(api.ruleObservations).mockResolvedValue([]);
  vi.mocked(api.ruleProposals).mockResolvedValue([]);
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
