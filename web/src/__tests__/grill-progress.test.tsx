import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GrillProgress } from "@/features/prds/GrillProgress";
import { PRD_SETTABLE_STATUSES, PRD_STATUS_ORDER } from "@/features/prds/meta";
import type { GrillState } from "@/lib/types";

/** Approval is shown as EARNED, not picked (AL-301 / PRD-15 D7).
 *
 *  Two things a reader has to be able to tell apart and previously could not: what is
 *  still open versus what the author deliberately deferred, and whether a real model
 *  graded the answers or the offline stub merely counted them. */

function state(over: Partial<GrillState> = {}): GrillState {
  const dims = {
    scope_edges: { outcome: "resolved", note: "local only", turn_seq: 1,
                   graded_by: "anthropic", question: "q" },
    failure_modes: { outcome: "resolved", note: "", turn_seq: 2,
                     graded_by: "anthropic", question: "q" },
    contracts: { outcome: "deferred", note: "wire format after the spike", turn_seq: 3,
                 graded_by: "author", question: "q" },
    open_decisions: { outcome: "unanswered", note: "", turn_seq: null,
                      graded_by: "", question: "q" },
  } as GrillState["dimensions"];
  return {
    prd_id: "AL-P15", turns: [], questions: 4, answers: 3, grilled: true,
    dimensions: dims, outstanding: ["open_decisions"], deferred: ["contracts"],
    complete: false, graded: true, ungraded_reason: "",
    stall: { answers_since_progress: 0, stalled: false, since_seq: 0 }, ...over,
  };
}

const grillDefer = vi.fn(async (_id: string, _dimension: string, _reason: string) => state());
vi.mock("@/lib/api", () => ({
  api: { grillDefer: (id: string, dimension: string, reason: string) => grillDefer(id, dimension, reason) },
}));

function show(s: GrillState = state()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <GrillProgress state={s} prdId="AL-P15" />
    </QueryClientProvider>,
  );
}

beforeEach(() => grillDefer.mockClear());

describe("grill progress (AL-301)", () => {
  it("shows every dimension with its outcome", () => {
    show();
    expect(screen.getByText("Scope edges")).toBeInTheDocument();
    expect(screen.getByText("Failure modes")).toBeInTheDocument();
    expect(screen.getByText("Contracts")).toBeInTheDocument();
    expect(screen.getByText("Open decisions")).toBeInTheDocument();
  });

  it("distinguishes a deliberate deferral from something still open", () => {
    show();
    // Both are "not answered", and conflating them is the exact failure PRD-15's
    // three-outcome design exists to prevent.
    expect(screen.getByText("deferred")).toBeInTheDocument();
    expect(screen.getByText("open")).toBeInTheDocument();
    expect(screen.getByText(/wire format after the spike/)).toBeInTheDocument();
  });

  it("counts progress while the grill is unfinished", () => {
    show();
    expect(screen.getByText("3/4")).toBeInTheDocument();
    expect(screen.getByText(/approves itself/)).toBeInTheDocument();
  });

  it("says approval was reached, not set", () => {
    const done = state({ complete: true, outstanding: [] });
    show(done);
    expect(screen.getByText("Approved by grilling")).toBeInTheDocument();
    expect(screen.getByText(/not by anyone setting a status/)).toBeInTheDocument();
  });

  it("flags a dimension the offline stub graded", () => {
    /** The one that protects a reader from over-trusting a default install: `stub`
     *  means an answer was recorded, NOT that it was any good. */
    const s = state();
    s.dimensions.scope_edges.graded_by = "stub";
    show(s);
    expect(screen.getByText("offline")).toBeInTheDocument();
  });

  it("does not flag dimensions a real provider graded", () => {
    show();
    expect(screen.queryByText("offline")).not.toBeInTheDocument();
  });

  it("warns on a completed grill that was graded offline", () => {
    const s = state({ complete: true, outstanding: [] });
    Object.values(s.dimensions).forEach((d) => { d.graded_by = "stub"; });
    show(s);
    expect(screen.getByText(/substance was not assessed/)).toBeInTheDocument();
  });
});

describe("the status control (AL-301)", () => {
  it("does not offer approved as a choice", () => {
    /** Offering it would be a control whose every use the server refuses with a 409. */
    expect(PRD_SETTABLE_STATUSES).toEqual(["draft", "review"]);
  });

  it("still knows how to render approved", () => {
    /** It remains a real status — it just isn't pickable. Dropping it from the display
     *  metadata would leave approved PRDs rendering as unknown. */
    expect(PRD_STATUS_ORDER).toContain("approved");
  });
});

describe("an ungraded round (GRPH-485, the read path)", () => {
  it("says the outcomes were not judged, and why", () => {
    /** The loop this ends: the grader is unreachable, the dimensions do not move, and
     *  the panel reports them exactly as it would for an answer that was too thin. The
     *  author answers again, and again. */
    show(state({
      graded: false,
      ungraded_reason: "the ollama grader could not be asked, or returned something unusable.",
    }));
    expect(screen.getByText("Not judged this round.")).toBeInTheDocument();
    expect(screen.getByText(/ollama grader could not be asked/)).toBeInTheDocument();
  });

  it("says nothing when the round was graded", () => {
    show();
    expect(screen.queryByText("Not judged this round.")).not.toBeInTheDocument();
  });
});

describe("deferring from the panel (AL-298)", () => {
  it("offers the exit only on what is still open", () => {
    /** A resolved or already-deferred dimension has nothing to defer. */
    show();
    expect(screen.getAllByText("defer")).toHaveLength(1);
  });

  it("sends the dimension and the author's reason", async () => {
    const user = userEvent.setup();
    show();
    await user.click(screen.getByText("defer"));
    await user.type(screen.getByRole("textbox"), "settling it needs the spike");
    await user.click(screen.getByRole("button", { name: "Defer" }));
    expect(grillDefer).toHaveBeenCalledWith("AL-P15", "open_decisions", "settling it needs the spike");
  });

  it("refuses a deferral with no reason behind it", async () => {
    /** A deferral is a decision that rides onto the baseline. Unexplained, it is the
     *  hand-waving the standard exists to catch — so the control will not fire. */
    const user = userEvent.setup();
    show();
    await user.click(screen.getByText("defer"));
    expect(screen.getByRole("button", { name: "Defer" })).toBeDisabled();
    await user.keyboard("{Enter}");
    expect(grillDefer).not.toHaveBeenCalled();
  });
});

describe("a grill that has stopped moving", () => {
  const stuck = { answers_since_progress: 4, stalled: true, since_seq: 2 };

  it("names the standstill and both real ways out", () => {
    show(state({ stall: stuck }));
    expect(screen.getByText("4 answers, nothing moved.")).toBeInTheDocument();
    // The two exits that exist. Approving is not among them, deliberately.
    expect(screen.getByText(/Defer a dimension with a reason/)).toBeInTheDocument();
    expect(screen.getByText(/chat model/)).toBeInTheDocument();
  });

  it("stays quiet while the grill is moving", () => {
    show();
    expect(screen.queryByText(/nothing moved/)).not.toBeInTheDocument();
  });

  it("does not blame the author for a grader that was never asked", () => {
    /** An ungraded round cannot move a dimension, so it looks exactly like a stall.
     *  Saying both points at the answers for something the outage did. */
    show(state({ stall: stuck, graded: false, ungraded_reason: "the ollama grader could not be asked." }));
    expect(screen.getByText("Not judged this round.")).toBeInTheDocument();
    expect(screen.queryByText(/nothing moved/)).not.toBeInTheDocument();
  });

  it("stays quiet once the grill is finished", () => {
    show(state({ stall: stuck, complete: true, outstanding: [] }));
    expect(screen.queryByText(/nothing moved/)).not.toBeInTheDocument();
  });
});
