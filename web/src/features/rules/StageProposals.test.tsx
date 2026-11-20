/** Proposals inbox and custom rules (GRPH-1781). */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { CustomRule, RuleProposal } from "@/lib/types";

import { StageProposals } from "./StageProposals";

vi.mock("@/lib/api", () => ({
  api: { ruleProposals: vi.fn(), customRules: vi.fn(), adoptRuleProposals: vi.fn(), dismissRuleProposal: vi.fn() },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core" }, projects: [], setActiveId: () => {} }),
}));

function prop(over: Partial<RuleProposal> & { id: number }): RuleProposal {
  return {
    project_id: "core", kind: "new_rule", rule_id: `custom.r${over.id}`, primitive: "p", draft: { stage: "plan", title: `Rule ${over.id}` },
    params: {}, citations: [{ kind: "file", ref: { path: "a/b.py", commit: "abcdef1234567890", lines: "3-9" } }, { kind: "item", ref: "GRPH-5" }],
    proposer: "alex", proposer_kind: "person", direction: "tighten", loosens: false, status: "open", folded: false,
    created_at: "2026-01-01T00:00:00Z", ...over,
  };
}
const custom: CustomRule = {
  rule_id: "custom.x", primitive: "p", stage: "plan", title: "No Friday merges", why: "outages", claims: "", params: {},
  state: "active", author: "sam", reason: "", created_at: "2026-01-01T00:00:00Z", on: true, source: "custom",
};

function view() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <StageProposals stageId="plan" ruleIds={[]} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.customRules).mockResolvedValue([]);
  vi.mocked(api.ruleProposals).mockResolvedValue([prop({ id: 1 }), prop({ id: 2 }), prop({ id: 3, loosens: true })]);
});

describe("StageProposals", () => {
  it("renders an open proposal with its citations", async () => {
    view();
    const row = await screen.findByTestId("proposal-1");
    expect(row).toHaveTextContent("Rule 1");
    expect(row).toHaveTextContent("a/b.py:3-9 @ abcdef123456");
    expect(row).toHaveTextContent("item GRPH-5");
    expect(screen.getByTestId("loosens-flag")).toBeInTheDocument();
  });

  it("batch adopt sends one reason; loosens checkbox is disabled with an explanation", async () => {
    vi.mocked(api.adoptRuleProposals).mockResolvedValue([]);
    view();
    await screen.findByTestId("proposal-1");
    const loose = screen.getByLabelText("Select proposal 3");
    expect(loose).toBeDisabled();
    expect(screen.getByTestId("proposal-3")).toHaveTextContent("can't join a batch");
    await userEvent.click(screen.getByLabelText("Select proposal 1"));
    await userEvent.click(screen.getByLabelText("Select proposal 2"));
    await userEvent.type(screen.getByLabelText("Batch reason"), "ok");
    await userEvent.click(screen.getByRole("button", { name: /Adopt selected \(2\)/ }));
    await waitFor(() => expect(api.adoptRuleProposals).toHaveBeenCalledWith("core", [1, 2], "ok"));
    expect(api.adoptRuleProposals).toHaveBeenCalledTimes(1);
  });

  it("dismiss requires a reason", async () => {
    vi.mocked(api.dismissRuleProposal).mockResolvedValue(prop({ id: 1 }));
    view();
    const row = await screen.findByTestId("proposal-1");
    const btn = row.querySelector("button") as HTMLButtonElement;
    expect(btn).toHaveTextContent("Dismiss");
    expect(btn).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Reason for proposal 1"), "noise");
    await userEvent.click(btn);
    await waitFor(() => expect(api.dismissRuleProposal).toHaveBeenCalledWith("core", 1, "noise"));
  });

  it("shows a refusal verbatim on a loosening Adopt", async () => {
    vi.mocked(api.adoptRuleProposals).mockRejectedValue(new Error(JSON.stringify({ detail: "refused: rules.person_adopts" })));
    view();
    await screen.findByTestId("proposal-3");
    await userEvent.type(screen.getByLabelText("Reason for proposal 3"), "because");
    await userEvent.click(screen.getByTestId("proposal-3").querySelectorAll("button")[0]);
    expect(await screen.findByTestId("proposal-error-3")).toHaveTextContent("refused: rules.person_adopts");
    expect(api.adoptRuleProposals).toHaveBeenCalledWith("core", [3], "because");
  });

  it("draws a custom rule with Custom badge, author and why", async () => {
    vi.mocked(api.ruleProposals).mockResolvedValue([]);
    vi.mocked(api.customRules).mockResolvedValue([custom]);
    view();
    const row = await screen.findByTestId("custom-rule-custom.x");
    expect(row).toHaveTextContent("Custom");
    expect(row).toHaveTextContent("No Friday merges");
    expect(row).toHaveTextContent("sam");
    expect(row).toHaveTextContent("Why: outages");
  });
});
