/** A custom rule is not a library rule (GRPH-1880). */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { RuleFlowRule, RuleStage } from "@/lib/types";

import { DockEditor } from "./DockEditor";

vi.mock("@/lib/api", () => ({
  api: {
    ruleToggles: vi.fn(), gitops: vi.fn(), me: vi.fn(), members: vi.fn(), syncStatus: vi.fn(), config: vi.fn(),
    customRules: vi.fn(), ruleProposals: vi.fn(), events: vi.fn(), ruleEffect: vi.fn(),
    ruleObservations: vi.fn(),
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

const lib = rule({ id: "plan.exclusive_areas", title: "Exclusive areas" });
const cust = rule({ id: "custom.walk-docs-exclusive", title: "Walk docs exclusive", value: false, used: false });
const stage = { id: "plan", title: "Plan", summary: "", state: "enforced", rules: [lib, cust] } as unknown as RuleStage;

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.me).mockResolvedValue({ id: "u1" } as never);
  vi.mocked(api.members).mockResolvedValue([{ user: { id: "u1" }, role: "owner" }] as never);
  vi.mocked(api.gitops).mockResolvedValue({ control: { state: "local", writable: true, message: "" } } as never);
  vi.mocked(api.config).mockResolvedValue({ hosted_mode: false } as never);
  vi.mocked(api.syncStatus).mockResolvedValue({ linked: false } as never);
  vi.mocked(api.ruleToggles).mockResolvedValue({ toggles: [] } as never);
  vi.mocked(api.ruleObservations).mockResolvedValue([] as never);
  vi.mocked(api.events).mockResolvedValue({ results: [] } as never);
  vi.mocked(api.ruleEffect).mockResolvedValue({ rules: [] } as never);
  vi.mocked(api.ruleProposals).mockResolvedValue([
    { id: 2, rule_id: "custom.walk-docs-exclusive", proposer: "PNW-A1", proposer_kind: "agent", citations: [] },
  ] as never);
  vi.mocked(api.customRules).mockResolvedValue([
    { rule_id: "custom.walk-docs-exclusive", stage: "Plan", title: "Walk docs exclusive", state: "observing", params: { paths: ["docs/walk/"] } },
  ] as never);
});

function view() {
  return render(
    <MemoryRouter>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <DockEditor stage={stage} version="1" model={null} focusRule={null} prefill={null} onClose={() => {}} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("DockEditor custom rules", () => {
  it("keeps custom.* out of LIBRARY RULES and its count", () => {
    view();
    const library = screen.getByTestId("rules-dock-group-library");
    expect(library).toHaveTextContent("LIBRARY RULES · 1");
    expect(library).not.toHaveTextContent("custom.");
    expect(screen.getByTestId("rules-dock-group-custom")).toHaveTextContent("custom.walk-docs-exclusive");
  });

  it("opens a custom view: custom/project, params, state, no On/Off/Inherit, adoption in history", async () => {
    const user = userEvent.setup();
    view();
    await user.click(screen.getByTestId("rules-dock-row-custom.walk-docs-exclusive"));
    expect(screen.getByTestId("rules-dock-kind")).toHaveTextContent("CUSTOM RULE");
    expect(screen.getByTestId("rules-dock-custom-source")).toHaveTextContent("custom/project");
    expect(await screen.findByTestId("rules-dock-custom-params")).toHaveTextContent("docs/walk/");
    expect(screen.getByTestId("rules-dock-state-badge")).toHaveTextContent("OBSERVING");
    expect(screen.queryByTestId("rules-dock-state")).toBeNull();
    expect(screen.queryByText(/platform default/i)).toBeNull();
    await waitFor(() => expect(screen.getByTestId("rules-history-adopted")).toHaveTextContent("proposed by Agent PNW-A1"));
  });

  it("points an observing custom rule at Promote and an active one at a proposal", async () => {
    const user = userEvent.setup();
    const first = view();
    await user.click(screen.getByTestId("rules-dock-row-custom.walk-docs-exclusive"));
    expect(await screen.findByTestId("rules-dock-state-badge")).toHaveTextContent("OBSERVING");
    expect(screen.getByTestId("rules-readonly-reason")).toHaveTextContent(
      "Custom rule: it has no library switch. Promote it while it observes.",
    );

    first.unmount();
    vi.mocked(api.customRules).mockResolvedValue([
      { rule_id: "custom.walk-docs-exclusive", stage: "Plan", title: "Walk docs exclusive", state: "active", params: {} },
    ] as never);
    view();
    await user.click(screen.getByTestId("rules-dock-row-custom.walk-docs-exclusive"));
    expect(await screen.findByTestId("rules-dock-state-badge")).toHaveTextContent("ACTIVE");
    const reason = screen.getByTestId("rules-readonly-reason");
    // This assertion fails when the state check is removed and the promote sentence stays.
    expect(reason).toHaveTextContent(
      "Custom rule: it has no library switch. It is changed by proposal, not here.",
    );
    expect(reason).not.toHaveTextContent(/promote/i);
  });

  it("does not tell the reader to promote a custom rule whose state was not read", async () => {
    vi.mocked(api.customRules).mockResolvedValue([] as never);
    const user = userEvent.setup();
    view();
    await user.click(screen.getByTestId("rules-dock-row-custom.walk-docs-exclusive"));
    expect(await screen.findByTestId("rules-dock-custom-state")).toHaveTextContent("State not read.");
    const reason = screen.getByTestId("rules-readonly-reason");
    expect(reason).toHaveTextContent("Custom rule: it has no library switch. State not read.");
    expect(reason).not.toHaveTextContent(/promote/i);
  });

  it("keeps the would-have record readable after promotion (D5)", async () => {
    vi.mocked(api.customRules).mockResolvedValue([
      { rule_id: "custom.walk-docs-exclusive", stage: "Plan", title: "Walk docs exclusive", state: "active", params: {} },
    ] as never);
    vi.mocked(api.ruleObservations).mockResolvedValue([
      { id: 7, rule_id: "custom.walk-docs-exclusive", outcome: "would_hold", item_id: "PNW-12", at: "2026-11-20T10:00:00Z" },
    ] as never);
    const user = userEvent.setup();
    view();
    await user.click(screen.getByTestId("rules-dock-row-custom.walk-docs-exclusive"));
    const record = await screen.findByTestId("would-have-custom.walk-docs-exclusive");
    await waitFor(() => expect(record).toHaveTextContent("Would have held PNW-12"));
    expect(screen.getByTestId("would-have-record-custom.walk-docs-exclusive")).toHaveTextContent("WOULD-HAVE RECORD (1)");
    expect(api.ruleObservations).toHaveBeenCalledWith("core", "custom.walk-docs-exclusive");
    expect(screen.queryByTestId("rules-dock-state")).toBeNull();
  });
});
