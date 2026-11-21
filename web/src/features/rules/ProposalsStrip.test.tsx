/** The proposals strip (GRPH-1807). */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { Event, RuleProposal } from "@/lib/types";

import { adoptedLine } from "./RuleHistory";
import { ProposalsStrip } from "./ProposalsStrip";

vi.mock("@/lib/api", () => ({
  api: {
    ruleProposals: vi.fn(), adoptRuleProposals: vi.fn(), dismissRuleProposal: vi.fn(), events: vi.fn(), me: vi.fn(), members: vi.fn(),
  },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core" }, projects: [], setActiveId: () => {} }),
}));
const canWrite = vi.hoisted(() => ({ value: true as boolean | null }));
vi.mock("./LibraryRuleControls", () => ({ useCanWriteRules: () => canWrite.value }));

function prop(over: Partial<RuleProposal> & { id: number }): RuleProposal {
  return {
    project_id: "core", kind: "new_rule", rule_id: `custom.r${over.id}`, primitive: "p", draft: { stage: "Plan", title: `Rule ${over.id}` },
    params: {}, citations: [{ kind: "item", ref: "GRPH-5" }, { kind: "item", ref: "GRPH-6" }],
    proposer: "agent-7", proposer_kind: "agent", direction: "tighten", loosens: false, status: "open", folded: false,
    created_at: "2026-01-01T00:00:00Z", ...over,
  };
}

/** An audit event as GET /events returns it: the decision's who/when/why live here, not on the proposal. */
function decided(action: string, proposalId: number, actor: string, reason: string, ts: string): Event {
  return {
    id: proposalId, ts, actor_type: "user", actor_id: `u-${actor}`, actor_label: actor, surface: "rest", action,
    target_type: "rule", target_id: `custom.r${proposalId}`, project_id: "core", meta: { proposal_id: proposalId, reason },
  };
}

function view(stageFilter: string | null = null) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ProposalsStrip stageFilter={stageFilter} onClearFilter={() => {}} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  canWrite.value = true;
  vi.mocked(api.ruleProposals).mockImplementation(async (_p, status) =>
    status === "dismissed"
      ? [prop({ id: 8, status: "dismissed" })] // the list route sends no decided_by / decided_at / decided_reason
      : [prop({ id: 1 }), prop({ id: 2 }), prop({ id: 3, loosens: true }), prop({ id: 4, draft: { stage: "Merge", title: "Rule 4" } })],
  );
  vi.mocked(api.events).mockImplementation(async (q) => ({
    results:
      q?.action === "dismiss_rule_proposal"
        ? [decided("dismiss_rule_proposal", 8, "Sam", "noise", new Date(Date.now() - 2 * 86400_000).toISOString())]
        : [],
    total: 1,
  }) as never);
});

describe("ProposalsStrip", () => {
  it("lists open proposals with nothing pre-ticked and no select all", async () => {
    view();
    expect(await screen.findByText("Proposals (4 open)")).toBeInTheDocument();
    for (const c of screen.getAllByRole("checkbox")) expect(c).not.toBeChecked();
    expect(screen.queryByText(/select all/i)).toBeNull();
    expect(screen.getByTestId("proposal-1")).toHaveTextContent("Agent agent-7");
    expect(screen.queryByTestId("batch-bar")).toBeNull();
  });

  it("filters to one stage", async () => {
    view("merge");
    await screen.findByTestId("proposal-4");
    expect(screen.queryByTestId("proposal-1")).toBeNull();
  });

  it("disables a loosening's checkbox, gives it its own Adopt with a reason, and leaves the others usable", async () => {
    vi.mocked(api.adoptRuleProposals).mockResolvedValue([]);
    view();
    await screen.findByTestId("proposal-3");
    const loose = screen.getByLabelText("Select proposal 3");
    expect(loose).toBeDisabled();
    expect(screen.getByTestId("proposal-3")).toHaveTextContent("can't join a batch");
    expect(screen.getByLabelText("Select proposal 1")).toBeEnabled();
    const adopt = screen.getByTestId("proposal-3").querySelector("button") as HTMLButtonElement;
    expect(adopt).toHaveTextContent("Adopt");
    expect(adopt).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Reason for proposal 3"), "because");
    await userEvent.click(adopt);
    await waitFor(() => expect(api.adoptRuleProposals).toHaveBeenCalledWith("core", [3], "because"));
  });

  it("batch adopt sends one request with one reason", async () => {
    vi.mocked(api.adoptRuleProposals).mockResolvedValue([]);
    view();
    await screen.findByTestId("proposal-1");
    await userEvent.click(screen.getByLabelText("Select proposal 1"));
    await userEvent.click(screen.getByLabelText("Select proposal 2"));
    await userEvent.type(screen.getByLabelText("Batch reason"), "ok");
    await userEvent.click(screen.getByRole("button", { name: "Adopt 2 proposals" }));
    await waitFor(() => expect(api.adoptRuleProposals).toHaveBeenCalledWith("core", [1, 2], "ok"));
    expect(api.adoptRuleProposals).toHaveBeenCalledTimes(1);
  });

  it("a refused batch keeps ticks and reason and shows the refusal verbatim", async () => {
    vi.mocked(api.adoptRuleProposals).mockRejectedValue(new Error(JSON.stringify({ detail: "refused: rules.person_adopts" })));
    view();
    await screen.findByTestId("proposal-1");
    await userEvent.click(screen.getByLabelText("Select proposal 1"));
    await userEvent.click(screen.getByLabelText("Select proposal 2"));
    await userEvent.type(screen.getByLabelText("Batch reason"), "ok");
    await userEvent.click(screen.getByRole("button", { name: "Adopt 2 proposals" }));
    expect(await screen.findByTestId("batch-error")).toHaveTextContent("refused: rules.person_adopts");
    expect(screen.getByLabelText("Select proposal 1")).toBeChecked();
    expect(screen.getByLabelText("Select proposal 2")).toBeChecked();
    expect(screen.getByLabelText("Batch reason")).toHaveValue("ok");
  });

  it("dismiss without a reason is refused client-side; the note says the proposer is not notified", async () => {
    vi.mocked(api.dismissRuleProposal).mockResolvedValue(prop({ id: 1 }));
    view();
    await screen.findByTestId("proposal-1");
    await userEvent.click(within(screen.getByTestId("proposal-1")).getByRole("button", { name: "Dismiss" }));
    expect(await screen.findByTestId("dismiss-sheet")).toHaveTextContent("the proposer is not notified");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss proposal" }));
    expect(await screen.findByTestId("dismiss-error")).toBeInTheDocument();
    expect(api.dismissRuleProposal).not.toHaveBeenCalled();
    await userEvent.type(screen.getByLabelText("Dismiss reason"), "noise");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss proposal" }));
    await waitFor(() => expect(api.dismissRuleProposal).toHaveBeenCalledWith("core", 1, "noise"));
  });

  it("lists dismissed proposals with who and why", async () => {
    view();
    await userEvent.click(await screen.findByTestId("dismissed-toggle"));
    await waitFor(() => expect(screen.getByTestId("dismissed-8")).toHaveTextContent("dismissed by Sam"));
    expect(screen.getByTestId("dismissed-8")).toHaveTextContent("2d ago");
    expect(screen.getByTestId("dismissed-8")).toHaveTextContent("noise");
    expect(api.events).toHaveBeenCalledWith(expect.objectContaining({ projectId: "core", action: "dismiss_rule_proposal" }));
  });

  it("every card has its own Adopt, which needs a reason", async () => {
    vi.mocked(api.adoptRuleProposals).mockResolvedValue([]);
    view();
    const card = within(await screen.findByTestId("proposal-1"));
    const adopt = card.getByRole("button", { name: "Adopt" });
    expect(adopt).toBeDisabled();
    await userEvent.type(card.getByLabelText("Reason for proposal 1"), "fine");
    await userEvent.click(adopt);
    await waitFor(() => expect(api.adoptRuleProposals).toHaveBeenCalledWith("core", [1], "fine"));
  });

  it("a batch keeps ticks hidden by the stage filter", async () => {
    vi.mocked(api.adoptRuleProposals).mockResolvedValue([]);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <ProposalsStrip stageFilter={null} onClearFilter={() => {}} />
      </QueryClientProvider>,
    );
    await screen.findByTestId("proposal-1");
    await userEvent.click(screen.getByLabelText("Select proposal 1"));
    await userEvent.click(screen.getByLabelText("Select proposal 4"));
    rerender(
      <QueryClientProvider client={qc}>
        <ProposalsStrip stageFilter="merge" onClearFilter={() => {}} />
      </QueryClientProvider>,
    );
    expect(screen.queryByTestId("proposal-1")).toBeNull();
    await userEvent.type(screen.getByLabelText("Batch reason"), "ok");
    await userEvent.click(screen.getByRole("button", { name: "Adopt 2 proposals" }));
    await waitFor(() => expect(api.adoptRuleProposals).toHaveBeenCalledWith("core", [1, 4], "ok"));
  });

  it("a member sees no checkbox, Adopt or Dismiss", async () => {
    canWrite.value = false;
    view();
    await screen.findByTestId("proposal-1");
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByRole("button", { name: /^(Adopt|Dismiss$)/ })).toBeNull();
  });

  it("history names the agent as proposer and the person as adopter", () => {
    const at = new Date(Date.now() - 3 * 3600_000).toISOString();
    const line = adoptedLine(prop({ id: 1, status: "adopted" }), { by: "Alex", at, reason: "ok" });
    expect(line).toContain("adopted by Alex · 3h ago");
    expect(line).toContain("proposed by Agent agent-7 (cited item GRPH-5 +1)");
  });
});
