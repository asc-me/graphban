import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApprovalEval } from "@/features/prds/ApprovalEval";
import type { PrdEval } from "@/lib/types";

const mechanical: PrdEval = {
  prd_id: "PRD-1",
  judged: false,
  ready: null,
  cause: "not_asked",
  ungraded_reason: "the judge has not been asked — ungraded is not a pass",
  mechanical_ready: false,
  completeness: [
    { dimension: "problem", label: "Problem", state: "present", section: "Problem" },
    { dimension: "scope", label: "Scope / Goals", state: "present", section: "Goals" },
    { dimension: "non_goals", label: "Non-goals", state: "present", section: "Non-Goals" },
    { dimension: "acceptance", label: "Acceptance criteria", state: "missing", section: null },
  ],
  missing: ["acceptance"],
  thin: [],
  coverage_gaps: ["Adapter"],
  empty_sections: [],
  shaped: true,
  implementable_sections: 1,
  coverage_note: "1 buildable section(s) have no items yet",
  ambiguous: [],
  untestable: [],
  callouts: ["add a Acceptance criteria section", "no work linked under Adapter"],
  judge_reason: "",
};

const { evaluateSpy, askSpy } = vi.hoisted(() => ({
  evaluateSpy: vi.fn(async (): Promise<PrdEval> => mechanical),
  askSpy: vi.fn(async (): Promise<PrdEval> => ({
    ...mechanical,
    judged: true,
    ready: false,
    cause: "ok",
    ungraded_reason: "",
    ambiguous: ["Goals: 'named models' is unspecified"],
    untestable: [],
    judge_reason: "acceptance is missing and goals are vague",
    callouts: [
      "add a Acceptance criteria section",
      "Goals: 'named models' is unspecified",
    ],
  })),
}));

vi.mock("@/lib/api", () => ({
  api: {
    prdEvaluate: evaluateSpy,
    prdEvaluateAsk: askSpy,
  },
}));

function renderPanel(grillComplete = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ApprovalEval prdId="PRD-1" grillComplete={grillComplete} />
    </QueryClientProvider>,
  );
}

describe("PRD approval eval (GRPH-80)", () => {
  it("names missing sections and ungraded, not a quiet ready", async () => {
    renderPanel();
    expect(await screen.findByText("Acceptance criteria")).toBeInTheDocument();
    expect(screen.getByText("missing")).toBeInTheDocument();
    expect(screen.getByText(/not judged — the judge has not been asked/)).toBeInTheDocument();
    expect(screen.getByText("ungraded")).toBeInTheDocument();
    expect(screen.queryByText("not ready")).not.toBeInTheDocument();
    expect(screen.getByText(/No work linked: Adapter/)).toBeInTheDocument();
  });

  it("asks the judge on demand and shows not-ready without looking like a score of 0", async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByText("ungraded");
    await user.click(screen.getByRole("button", { name: /Ask the judge/ }));
    expect(askSpy).toHaveBeenCalledWith("PRD-1");
    expect(await screen.findByText("not ready")).toBeInTheDocument();
    expect(screen.getByText(/Ambiguous:/)).toBeInTheDocument();
    expect(screen.queryByText("ungraded")).not.toBeInTheDocument();
  });

  it("warns when the grill approved without asking the judge", async () => {
    renderPanel(true);
    expect(
      await screen.findByText(/The grill approved this; the quality judge was not asked/),
    ).toBeInTheDocument();
  });
});
