/** Stage count row and custom rules (GRPH-1781, GRPH-1807). */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { CustomRule, RuleProposal } from "@/lib/types";

import { StageProposals } from "./StageProposals";

vi.mock("@/lib/api", () => ({ api: { ruleProposals: vi.fn(), customRules: vi.fn() } }));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core" }, projects: [], setActiveId: () => {} }),
}));

function prop(over: Partial<RuleProposal> & { id: number }): RuleProposal {
  return {
    project_id: "core", kind: "new_rule", rule_id: `custom.r${over.id}`, primitive: "p", draft: { stage: "plan", title: `Rule ${over.id}` },
    params: {}, citations: [{ kind: "item", ref: "GRPH-5" }],
    proposer: "agent-7", proposer_kind: "agent", direction: "tighten", loosens: false, status: "open", folded: false,
    created_at: "2026-01-01T00:00:00Z", ...over,
  };
}
const custom: CustomRule = {
  rule_id: "custom.x", primitive: "p", stage: "plan", title: "No Friday merges", why: "outages", claims: "", params: {},
  state: "active", author: "sam", reason: "", created_at: "2026-01-01T00:00:00Z", on: true, source: "custom",
};

function view(onReview = vi.fn()) {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <StageProposals stageId="plan" ruleIds={[]} onReview={onReview} />
    </QueryClientProvider>,
  );
  return onReview;
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.customRules).mockResolvedValue([]);
  vi.mocked(api.ruleProposals).mockResolvedValue([prop({ id: 1 }), prop({ id: 2 })]);
});

describe("StageProposals", () => {
  it("shows a count row and no Adopt or Dismiss; the link opens the strip for the stage", async () => {
    const onReview = view();
    expect(await screen.findByText(/2 proposed/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Adopt|Dismiss/ })).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Review in proposals" }));
    expect(onReview).toHaveBeenCalledWith("plan");
  });

  it("says No proposals at zero", async () => {
    vi.mocked(api.ruleProposals).mockResolvedValue([]);
    view();
    expect(await screen.findByText("No proposals")).toBeInTheDocument();
  });

  it("shows — and Retry when proposals cannot load, never 0", async () => {
    vi.mocked(api.ruleProposals).mockRejectedValue(new Error("boom"));
    view();
    expect(await screen.findByText(/Proposals could not be loaded/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByTestId("stage-proposals-count")).toHaveTextContent("—");
    expect(screen.getByTestId("stage-proposals-count")).not.toHaveTextContent("0");
  });

  it("reads 'by <person>' for an adopted custom rule, never the proposing agent", async () => {
    vi.mocked(api.customRules).mockResolvedValue([{ ...custom, author: "agent-7" }]);
    vi.mocked(api.ruleProposals).mockImplementation(async (_p, status) =>
      status === "adopted" ? [prop({ id: 9, rule_id: "custom.x", status: "adopted", decided_by: "Alex" })] : [],
    );
    view();
    const row = await screen.findByTestId("custom-rule-custom.x");
    await vi.waitFor(() => expect(row).toHaveTextContent("by Alex"));
    expect(row).not.toHaveTextContent("agent-7");
    expect(row).toHaveTextContent("Why: outages");
  });

  it("does not name an agent author when no person is known", async () => {
    vi.mocked(api.customRules).mockResolvedValue([{ ...custom, author: "agent-7" }]);
    vi.mocked(api.ruleProposals).mockImplementation(async (_p, status) =>
      status === "adopted" ? [prop({ id: 9, rule_id: "custom.x", status: "adopted", decided_by: null })] : [],
    );
    view();
    const row = await screen.findByTestId("custom-rule-custom.x");
    await vi.waitFor(() => expect(row).toHaveTextContent("by unknown"));
    expect(row).not.toHaveTextContent("agent-7");
  });
});
