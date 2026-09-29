/**
 * PRD-47 §S3 — the dashboard's content, and the empty state it has never had.
 *
 * S1 gave this view an error branch and a skeleton (`absence-vs-failure.test.tsx` holds
 * those). What was still missing is the third answer: a project with genuinely nothing in
 * it. Without it the view drew six zero tiles, and a zero tile is a *measurement* — it
 * says the count was read and came back empty. For a project nobody has used, the same
 * zeros read as "we looked and found nothing to report", which is the absence-as-clean
 * defect §2.1 names.
 *
 * So the cases here come in pairs: what renders when there is something, and what renders
 * when there is not. The pair that matters most is the last one — a failed fetch must
 * report a failure, not an empty project, and adding an empty state is exactly how a view
 * acquires a second wrong answer to a failed request.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DashboardView } from "@/features/dashboard/DashboardView";
import { ProjectProvider } from "@/features/ProjectContext";
import { STATUS_META, STATUS_ORDER, TYPE_META } from "@/lib/meta";
import type { DashboardData, RequestType } from "@/lib/types";

const { dashboardSpy } = vi.hoisted(() => ({ dashboardSpy: vi.fn() }));

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [
      { id: "core", name: "Core", tag: "GRPH", accent: "#c6f24e", description: "" },
    ]),
    config: vi.fn(async () => ({ hosted_mode: false })),
    dashboard: dashboardSpy,
  },
}));

/** A project with work in it. `next` is deliberately zero — see the legend case. */
const populated: DashboardData = {
  items_total: 11,
  items_by_status: { backlog: 3, next: 0, in_progress: 4, review: 1, done: 2, blocked: 1 },
  effort_total: 40,
  done_count: 2,
  in_progress_count: 4,
  blocked_count: 1,
  requests_total: 5,
  requests_by_type: { bug: 2, feature: 3, enhancement: 0, feedback: 0 },
  requests_by_status: { new: 3, triaging: 1, linked: 1 },
  shard_count: 7,
  prd_count: 2,
  mcp_calls: 4210,
  recent_items: [
    { id: "GRPH-954", title: "Plan the surfaces", status: "in_progress", date: "Sep 28" },
    { id: "GRPH-952", title: "State floor", status: "done", date: "Sep 27" },
  ],
};

/** Nothing at all: the only shape that may answer "empty project". */
const bare: DashboardData = {
  items_total: 0,
  items_by_status: { backlog: 0, next: 0, in_progress: 0, review: 0, done: 0, blocked: 0 },
  effort_total: 0,
  done_count: 0,
  in_progress_count: 0,
  blocked_count: 0,
  requests_total: 0,
  requests_by_type: {},
  requests_by_status: {},
  shard_count: 0,
  prd_count: 0,
  mcp_calls: 0,
  recent_items: [],
};

function renderDashboard() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ProjectProvider>
          <DashboardView />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The panel a section title belongs to — titles are unique, panels are not addressable. */
function panelOf(title: string) {
  return screen.getByText(title).parentElement!;
}

beforeEach(async () => {
  vi.clearAllMocks();
  localStorage.clear();
  const { api } = await import("@/lib/api");
  vi.mocked(api.dashboard).mockResolvedValue(populated as never);
});

describe("Dashboard (PRD-47 S3)", () => {
  it("draws the six KPIs, each with an icon and its own value", async () => {
    renderDashboard();
    await screen.findByText("Items");

    const grid = screen.getByText("Items").parentElement!.parentElement!;
    expect(grid.children).toHaveLength(6);

    const tile = (label: string) => within(grid).getByText(label).parentElement!;
    expect(tile("Items")).toHaveTextContent("11");
    expect(tile("In progress")).toHaveTextContent("4");
    expect(tile("Blocked")).toHaveTextContent("1");
    expect(tile("Memory shards")).toHaveTextContent("7");
    expect(tile("PRDs")).toHaveTextContent("2");
    // Thousands are abbreviated rather than printed in full in a 22px tile.
    expect(tile("MCP calls")).toHaveTextContent("4.2k");

    for (const t of Array.from(grid.children)) {
      expect(t.querySelector("svg")).toBeInTheDocument();
    }
  });

  it("segments the status bar by what is there and keeps the full vocabulary in the legend", async () => {
    renderDashboard();
    await screen.findByText("Item status distribution");
    const panel = panelOf("Item status distribution");

    // One segment per status that has items — `next` has none, so it is not drawn as a
    // zero-width sliver pretending to be a category.
    const segments = within(panel).getAllByTitle(/^[A-Za-z ]+: \d+$/);
    expect(segments).toHaveLength(5);
    expect(within(panel).getByTitle("In Progress: 4")).toBeInTheDocument();
    expect(within(panel).queryByTitle(/^Next:/)).not.toBeInTheDocument();

    // The legend is the vocabulary, so it lists every status including the empty one.
    // Dropping `Next` here would make a status with nothing in it unreadable — you could
    // not tell "no items are queued" from "this project has no such state".
    for (const s of STATUS_ORDER) {
      expect(within(panel).getByText(STATUS_META[s].label)).toBeInTheDocument();
    }
    const nextRow = within(panel).getByText("Next").parentElement!;
    expect(nextRow).toHaveTextContent("0");
  });

  it("lists every request type, including the ones nobody has filed", async () => {
    renderDashboard();
    await screen.findByText("Requests by type");
    const panel = panelOf("Requests by type");

    for (const t of Object.keys(TYPE_META) as RequestType[]) {
      expect(within(panel).getByText(TYPE_META[t].label)).toBeInTheDocument();
    }
    expect(within(panel).getByText("FEATURE").closest("div.flex")).toHaveTextContent("3");
    expect(within(panel).getByText("ENHANCEMENT").closest("div.flex")).toHaveTextContent("0");
  });

  it("lists recent activity with each item's id, title and date", async () => {
    renderDashboard();
    await screen.findByText("Recent activity");
    const panel = panelOf("Recent activity");

    expect(within(panel).getByText("GRPH-954")).toBeInTheDocument();
    expect(within(panel).getByText("Plan the surfaces")).toBeInTheDocument();
    expect(within(panel).getByText("Sep 28")).toBeInTheDocument();
    expect(within(panel).getByText("GRPH-952")).toBeInTheDocument();
  });

  it("lets the agent-loop card be dismissed, and keeps it dismissed", async () => {
    const { unmount } = renderDashboard();
    await screen.findByText("Put agents on the backlog");

    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("Put agents on the backlog")).not.toBeInTheDocument();

    // The card is guidance, not data, so dismissing it has to survive a reload — otherwise
    // the control only ever clears the current paint.
    unmount();
    renderDashboard();
    await screen.findByText("Items");
    expect(screen.queryByText("Put agents on the backlog")).not.toBeInTheDocument();
  });
});

describe("an empty project is not a measured one", () => {
  it("says nothing has been recorded instead of drawing six zero tiles", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.dashboard).mockResolvedValue(bare as never);
    renderDashboard();

    expect(await screen.findByText("Nothing to summarise yet")).toBeInTheDocument();
    // The tiles are the tell: a strip of zeros is a claim about the project.
    expect(screen.queryByText("Memory shards")).not.toBeInTheDocument();
    expect(screen.queryByText("Item status distribution")).not.toBeInTheDocument();
    expect(screen.queryByText("Recent activity")).not.toBeInTheDocument();
  });

  /**
   * Each clause in `nothingToSummarise` needs a case where THAT count alone is
   * non-zero. A fixture that sets two counts at once (the previous "5 requests
   * and 12 MCP calls") does not pin either clause: deleting
   * `data.requests_total === 0` still passed because mcp_calls kept the project
   * non-empty, so shards-only or PRDs-only read as an empty project.
   */
  it.each([
    { field: "items_total" as const, value: 3 },
    { field: "requests_total" as const, value: 5 },
    { field: "shard_count" as const, value: 2 },
    { field: "prd_count" as const, value: 1 },
    { field: "mcp_calls" as const, value: 12 },
  ])("still draws the tiles when only $field is non-zero", async ({ field, value }) => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.dashboard).mockResolvedValue({
      ...bare,
      [field]: value,
    } as never);
    renderDashboard();

    await screen.findByText("Items");
    expect(screen.queryByText("Nothing to summarise yet")).not.toBeInTheDocument();
    expect(screen.getByText("Memory shards")).toBeInTheDocument();
    expect(screen.getByText("PRDs")).toBeInTheDocument();
  });

  it("names a failed fetch instead of reporting an empty project", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.dashboard).mockRejectedValue(new Error("boom"));
    renderDashboard();

    expect(await screen.findByText(/the request failed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    // The new empty state must not become a second wrong answer to a failed request.
    expect(screen.queryByText("Nothing to summarise yet")).not.toBeInTheDocument();
    expect(screen.queryByText("Memory shards")).not.toBeInTheDocument();
  });
});
