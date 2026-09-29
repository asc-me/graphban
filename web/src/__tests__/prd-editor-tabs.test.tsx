import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";

/** The PRD editor's tab strip, rendered (PRD-47 S4 / GRPH-981).
 *
 *  S4 puts six tabs over the existing editor and the slice that shipped them was bounced
 *  for a reason worth stating plainly: it matched the spec on inspection and NOTHING in
 *  the web suite failed when a tab was deleted. `CoveragePanel` and `PrototypeRow` had
 *  tests that reached them by importing the component directly, which is how a whole
 *  surface can be covered and still have no test that renders it.
 *
 *  So these assert on the ids the tabs are keyed by, in the DOM, and on what each one
 *  mounts — the wiring, not the panels. The panels keep their own tests: acceptance copy
 *  in `acceptance-panel.test.tsx`, grill copy in `grill-progress.test.tsx`. */

// Not under test here and not cheap to draw: both stream from the API on mount. What IS
// under test is that the tab that owns them mounts them, so each stub is identifiable.
vi.mock("@/features/assistant/AssistantPanel", () => ({
  AssistantPanel: () => <div>assistant panel</div>,
}));
vi.mock("@/features/prds/GrillPanel", () => ({
  GrillPanel: () => <div>grill panel</div>,
}));
vi.mock("@/features/prds/ApprovalEval", () => ({
  ApprovalEval: () => <div>approval eval</div>,
}));

const mocks = vi.hoisted(() => ({
  prd: vi.fn(),
  prdVersions: vi.fn(),
  grillState: vi.fn(),
  intentDiff: vi.fn(),
  prdCoverage: vi.fn(),
  closeReport: vi.fn(),
  prdEvidence: vi.fn(),
  auditCoverage: vi.fn(),
  items: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [{ id: "core", name: "Core", tag: "GRPH" }]),
    platform: vi.fn(async () => ({ effective_chat_provider: "anthropic" })),
    prd: mocks.prd,
    prdVersions: mocks.prdVersions,
    grillState: mocks.grillState,
    intentDiff: mocks.intentDiff,
    prdCoverage: mocks.prdCoverage,
    closeReport: mocks.closeReport,
    prdEvidence: mocks.prdEvidence,
    auditCoverage: mocks.auditCoverage,
    items: mocks.items,
  },
}));

vi.mock("@/lib/publicApi", () => ({
  publicApi: { uploadAttachment: vi.fn() },
}));

const { PrdEditorView } = await import("@/features/prds/PrdEditorView");

/** The six S4 names, in the order the spec lists them. */
const TAB_IDS = ["preview", "assistant", "grill", "coverage", "acceptance", "history"];

function grillState() {
  return {
    prd_id: "GRPH-P47", turns: [], questions: 4, answers: 3, grilled: true,
    dimensions: {
      scope_edges: { outcome: "resolved", note: "", turn_seq: 1, graded_by: "anthropic", question: "q" },
      failure_modes: { outcome: "resolved", note: "", turn_seq: 2, graded_by: "anthropic", question: "q" },
      contracts: { outcome: "deferred", note: "after the spike", turn_seq: 3, graded_by: "author", question: "q" },
      open_decisions: { outcome: "unanswered", note: "", turn_seq: null, graded_by: "", question: "q" },
    },
    outstanding: ["open_decisions"], deferred: ["contracts"],
    complete: false, graded: true, ungraded_reason: "",
    stall: { answers_since_progress: 0, stalled: false, since_seq: 0 },
  };
}

async function show() {
  mocks.prd.mockResolvedValue({
    id: "GRPH-P47", title: "Design rework", status: "review", version: "v1.2",
    linked: [], updated_at: "2026-09-01", project_id: "core",
    body: "## S4\n\nSix tabs.", created_at: "2026-08-01",
  });
  mocks.prdVersions.mockResolvedValue([
    { id: "v1", version: "v1.0", note: "first baseline", date: "2026-08-01", body: "## S4" },
  ]);
  mocks.grillState.mockResolvedValue(grillState());
  mocks.intentDiff.mockResolvedValue(null);
  mocks.items.mockResolvedValue([]);
  mocks.prdCoverage.mockResolvedValue({
    prd_id: "GRPH-P47", title: "Design rework", status: "review", sections: [],
    section_count: 0, implementable_sections: 0, sections_with_tasks: 0, gaps: [],
    shaped: true, empty_sections: [], total_items: 0, done_items: 0, percent_done: 0,
    open_high_fidelity: 0,
  });
  // One section dropped from the baseline, so the acceptance tab has something to say.
  mocks.closeReport.mockResolvedValue({
    governed: true, original_version: "v1.0", governing_version: "v1.2",
    sections: [{
      section: "Judging", current_title: "Judging", introduced_at: "v1.0", dropped_at: "v1.1",
      framing: false, fate: "dropped", delivered_items: [], planned_items: [], history: [],
      disposition: null,
    }],
    dropped: ["Judging"], never_delivered: [], expanded_scope: [], added_after_approval: [],
    drift: { accumulated: 0, current: 0, total: 0 }, closed: null,
  });
  mocks.prdEvidence.mockResolvedValue({
    governed: true, baseline_version: "v1.2", sections: [], unsupported: [], uncorroborated: [],
  });
  mocks.auditCoverage.mockResolvedValue({ governed: true, covered: [], uncovered: [] });

  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/prds/GRPH-P47"]}>
        <ProjectProvider>
          <Routes>
            <Route path="/prds/:id" element={<PrdEditorView />} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  // The editor draws "Loading…" until the PRD resolves; the strip is the loaded state.
  await screen.findByTestId("prd-editor-tabs");
}

function tabIds() {
  const strip = screen.getByTestId("prd-editor-tabs");
  return within(strip)
    .getAllByTestId("prd-editor-tab")
    .map((el) => el.getAttribute("data-tab"));
}

async function openTab(id: string) {
  const user = userEvent.setup();
  const strip = screen.getByTestId("prd-editor-tabs");
  await user.click(within(strip).getByRole("button", { name: labelFor(id) }));
}

function labelFor(id: string) {
  return { preview: "Preview", assistant: "Assistant", grill: "Grill", coverage: "Coverage",
           acceptance: "Acceptance", history: "History" }[id] as string;
}

beforeEach(() => {
  Object.values(mocks).forEach((m) => m.mockClear());
});

describe("the PRD editor's six tabs (PRD-47 S4)", () => {
  it("renders the six ids, in the order the spec lists them", async () => {
    await show();
    expect(tabIds()).toEqual(TAB_IDS);
  });

  it("mounts the acceptance panel on the acceptance tab", async () => {
    await show();
    await openTab("acceptance");
    // The real AcceptancePanel, not a stub: this fails if the tab stops mounting it.
    expect(await screen.findByText(/Delivered vs original intent/)).toBeInTheDocument();
    expect(screen.getByText("CUT FROM SPEC")).toBeInTheDocument();
  });

  it("mounts a pane for every other tab, so none is a dead button", async () => {
    await show();

    await openTab("grill");
    expect(await screen.findByText("grill panel")).toBeInTheDocument();

    await openTab("coverage");
    expect(await screen.findByText(/sections covered/)).toBeInTheDocument();

    await openTab("history");
    expect(await screen.findByText("Version history")).toBeInTheDocument();

    await openTab("assistant");
    expect(await screen.findByText("assistant panel")).toBeInTheDocument();
  });

  it("shows the earned-approval line on the grill tab, where S4 puts it", async () => {
    /** Asserted through the EDITOR, not only against `GrillProgress` on its own: the
     *  panel has thorough tests and the call site that mounts it had none, so deleting
     *  `{grill && <GrillProgress …/>}` would have left the suite green. */
    await show();
    await openTab("grill");
    expect(await screen.findByText(
      "Approval is earned, not picked — Approved unlocks when every question has an answer the eval accepts.",
    )).toBeInTheDocument();
    expect(screen.getByText("3 / 4 answered")).toBeInTheDocument();
  });
});
