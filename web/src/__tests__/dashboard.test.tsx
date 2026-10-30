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

import { ProjectHome } from "@/features/projecthome/ProjectHome";
import { ProjectProvider } from "@/features/ProjectContext";
import { STATUS_META, STATUS_ORDER, TYPE_META } from "@/lib/meta";
import type { DashboardData, RequestType } from "@/lib/types";

const { dashboardSpy } = vi.hoisted(() => ({ dashboardSpy: vi.fn() }));

const countsPopulated = {
  items: 11, items_in_progress: 4, requests: 5, review: 2, triage_new: 1, lessons_published: 3, live_agents: 2,
};
const countsZero = {
  items: 0, items_in_progress: 0, requests: 0, review: 0, triage_new: 0, lessons_published: 0, live_agents: 0,
};
const mapOf = (node_count: number) => ({
  nodes: [], edges: [], node_count, edge_count: 0, outbound: [],
});
const fleetOf = (online: number) => ({
  agents: [], online, total: online, by_role: {}, posture: "single-agent", roles: [],
  presence_ttl_seconds: 150, heartbeat_interval_seconds: 50, review_queue: [],
  clusters: [], seats: [], waves: [],
});

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [
      { id: "core", name: "Core", tag: "GRPH", accent: "#c6f24e", description: "" },
    ]),
    config: vi.fn(async () => ({ hosted_mode: false })),
    dashboard: dashboardSpy,
    counts: vi.fn(),
    codeMap: vi.fn(),
    fleet: vi.fn(),
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
          <ProjectHome />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The panel a section title belongs to — titles are unique, panels are not addressable. */
function panelOf(title: string) {
  return screen.getByRole("heading", { name: title }).closest("section")!;
}

beforeEach(async () => {
  vi.clearAllMocks();
  localStorage.clear();
  const { api } = await import("@/lib/api");
  vi.mocked(api.dashboard).mockResolvedValue(populated as never);
  vi.mocked(api.counts).mockResolvedValue(countsPopulated as never);
  vi.mocked(api.codeMap).mockResolvedValue(mapOf(40) as never);
  vi.mocked(api.fleet).mockResolvedValue(fleetOf(2) as never);
});

/** The empty-project answer requires every source to be a measured zero, not only the dashboard. */
async function zeroTheRest() {
  const { api } = await import("@/lib/api");
  vi.mocked(api.counts).mockResolvedValue(countsZero as never);
  vi.mocked(api.codeMap).mockResolvedValue(mapOf(0) as never);
  vi.mocked(api.fleet).mockResolvedValue(fleetOf(0) as never);
}

describe("Dashboard (PRD-47 S3)", () => {
  it("draws the six stats in one grid, toned when they are above zero", async () => {
    renderDashboard();
    await screen.findByText("Items", { selector: "span" });

    const grid = screen.getByText("Items", { selector: "span" }).parentElement!.parentElement!;
    expect(grid.children).toHaveLength(6);

    const tile = (label: string) => within(grid).getByText(label).parentElement!;
    expect(tile("Items")).toHaveTextContent("11");
    expect(tile("In flight")).toHaveTextContent("4");
    expect(tile("In flight").querySelector(".text-accent")).toHaveTextContent("4");
    expect(tile("Blocked")).toHaveTextContent("1");
    expect(tile("Blocked").querySelector(".text-st-blocked")).toHaveTextContent("1");
    expect(tile("Memory shards")).toHaveTextContent("7");
    expect(tile("Graph nodes")).toHaveTextContent("40");
    expect(tile("Agents live")).toHaveTextContent("2");
    expect(tile("Agents live").querySelector(".text-st-done")).toHaveTextContent("2");
    // PRDs and MCP calls left the grid. PRDs remains a jump tile.
    expect(within(grid).queryByText("PRDs")).not.toBeInTheDocument();
    expect(within(grid).queryByText("MCP calls")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /PRDs/ })).toHaveTextContent("2 docs");

    for (const t of Array.from(grid.children)) {
      expect(t.querySelector("svg")).not.toBeInTheDocument();
    }
  });

  it("shows a failed count as failed, not 0", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.counts).mockRejectedValue(new Error("boom"));
    renderDashboard();
    const tile = (await screen.findByText("Agents live", { selector: "span" })).parentElement!;
    expect(tile).toHaveTextContent("—");
    expect(tile).not.toHaveTextContent("0");
    expect(tile.querySelector("[title]")).toHaveAttribute("title", "Not read — the request failed");
  });

  it("shows each stat's uppercase label above its value", async () => {
    renderDashboard();
    const label = await screen.findByText("Items", { selector: "span" });
    expect(label).toHaveClass("uppercase");
    expect(label.nextElementSibling).toHaveTextContent("11");
  });

  it("puts the four section headings and subtitles outside the cards", async () => {
    renderDashboard();
    await screen.findByText("Where the stream stands.");
    for (const [title, subtitle] of [
      ["Items", "Where the stream stands."],
      ["Requests", "From the public form, by type."],
      ["Recent activity", "Latest moves in the stream."],
      ["Jump to", "Live counts from each surface."],
    ]) {
      const heading = screen.getByRole("heading", { name: title });
      const sub = screen.getByText(subtitle);
      expect(sub.parentElement).toBe(heading.parentElement);
      expect(heading.closest(".rounded-\\[14px\\]")).toBeNull();
    }
  });

  it("orders the Jump to tiles Tracker through Live", async () => {
    renderDashboard();
    await screen.findByText("Where the stream stands.");
    const jump = screen.getByRole("region", { name: "Jump to" });
    const names = within(jump).getAllByRole("link").map((a) => a.textContent!.match(/^[A-Za-z ]+?(?=\d|—|not|$)/)![0].trim());
    expect(names).toEqual(["Tracker", "Triage", "PRDs", "Code graph", "Memory", "Lessons", "Fleet", "Live"]);
  });

  it("segments the status bar by what is there and keeps the full vocabulary in the legend", async () => {
    renderDashboard();
    await screen.findByRole("heading", { name: "Items" });
    const panel = panelOf("Items");

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
    await screen.findByRole("heading", { name: "Requests" });
    const panel = panelOf("Requests");

    for (const t of Object.keys(TYPE_META) as RequestType[]) {
      const label = TYPE_META[t].label;
      expect(within(panel).getByText(label.charAt(0) + label.slice(1).toLowerCase())).toBeInTheDocument();
    }
    expect(within(panel).getByText("Feature").closest("div.flex")).toHaveTextContent("3");
    expect(within(panel).getByText("Enhancement").closest("div.flex")).toHaveTextContent("0");
  });

  it("lists recent activity with each item's id, title and date", async () => {
    renderDashboard();
    await screen.findByRole("heading", { name: "Recent activity" });
    const panel = panelOf("Recent activity");

    expect(within(panel).getByText("GRPH-954")).toBeInTheDocument();
    expect(within(panel).getByText("Plan the surfaces")).toBeInTheDocument();
    expect(within(panel).getByText("Sep 28")).toBeInTheDocument();
    expect(within(panel).getByText("GRPH-952")).toBeInTheDocument();
    // Rows open the tracker. Self-host paths are flat.
    expect(within(panel).getByText("GRPH-954").closest("a")).toHaveAttribute("href", "/tracker");
    expect(within(panel).getByText("GRPH-952").closest("a")).toHaveAttribute("href", "/tracker");
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
    await screen.findByText("Items", { selector: "span" });
    expect(screen.queryByText("Put agents on the backlog")).not.toBeInTheDocument();
  });
});

describe("an empty project is not a measured one", () => {
  it("says nothing has been recorded instead of drawing six zero tiles", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.dashboard).mockResolvedValue(bare as never);
    await zeroTheRest();
    renderDashboard();

    expect(await screen.findByText("Nothing to summarise yet")).toBeInTheDocument();
    // The tiles are the tell: a strip of zeros is a claim about the project.
    expect(screen.queryByText("Memory shards")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Items" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Recent activity" })).not.toBeInTheDocument();
  });

  /**
   * Each member of the empty-project check needs a case where THAT count alone is
   * non-zero. A fixture that sets two counts at once does not pin either clause:
   * deleting one comparison still passed because the other count kept the project
   * non-empty, so shards-only or PRDs-only read as an empty project.
   *
   * mcp_calls is not a member. The dashboard fills it from the deployment's total,
   * and the project-home suite is what pins that a positive figure still leaves
   * the page-wide empty state.
   */
  it.each([
    { field: "items_total" as const, value: 3 },
    { field: "in_progress_count" as const, value: 3 },
    { field: "blocked_count" as const, value: 1 },
    { field: "requests_total" as const, value: 5 },
    { field: "shard_count" as const, value: 2 },
    { field: "prd_count" as const, value: 1 },
  ])("still draws the tiles when only $field is non-zero", async ({ field, value }) => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.dashboard).mockResolvedValue({
      ...bare,
      [field]: value,
    } as never);
    // Counts, the map, and the fleet have to be zero too. A non-zero beforeEach
    // count would keep the page populated even if this dashboard clause were deleted.
    await zeroTheRest();
    renderDashboard();

    await screen.findByText("Items", { selector: "span" });
    expect(screen.queryByText("Nothing to summarise yet")).not.toBeInTheDocument();
    expect(screen.getByText("Memory shards")).toBeInTheDocument();
    expect(screen.getByText("PRDs")).toBeInTheDocument();
  });

  it.each([
    { field: "lessons_published" as const, value: 4, meta: "4 published" },
    { field: "triage_new" as const, value: 2, meta: "2 new" },
    { field: "review" as const, value: 6, meta: "6 to review" },
  ])("still draws the home when only counts.$field is non-zero", async ({ field, value, meta }) => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.dashboard).mockResolvedValue(bare as never);
    await zeroTheRest();
    vi.mocked(api.counts).mockResolvedValue({ ...countsZero, [field]: value } as never);
    renderDashboard();

    await screen.findByText("Items", { selector: "span" });
    expect(screen.queryByText("Nothing to summarise yet")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Jump to" })).toHaveTextContent(meta);
  });

  it("is not empty when the shell item count is non-zero and the dashboard said 0", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.dashboard).mockResolvedValue(bare as never);
    await zeroTheRest();
    vi.mocked(api.counts).mockResolvedValue({ ...countsZero, items: 8 } as never);
    renderDashboard();

    await screen.findByText("Items", { selector: "span" });
    expect(screen.queryByText("Nothing to summarise yet")).not.toBeInTheDocument();
    // The figure prefers the dashboard's measured 0. The shell's 8 still blocks the
    // empty project, which is what deleting that clause would bring back.
    expect(screen.getByText("Items", { selector: "span" }).parentElement).toHaveTextContent("0");
  });

  it("is not empty when the shell says work is in flight and the dashboard said 0", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.dashboard).mockResolvedValue(bare as never);
    await zeroTheRest();
    vi.mocked(api.counts).mockResolvedValue({ ...countsZero, items_in_progress: 3 } as never);
    renderDashboard();

    await screen.findByText("Items", { selector: "span" });
    expect(screen.queryByText("Nothing to summarise yet")).not.toBeInTheDocument();
    // The figure prefers the dashboard's measured 0. The shell's 3 still blocks the
    // empty project, which is what deleting that clause would bring back.
    expect(screen.getByText("In flight").parentElement).toHaveTextContent("0");
  });

  it("still draws the home when only the code graph answered with nodes", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.dashboard).mockResolvedValue(bare as never);
    await zeroTheRest();
    vi.mocked(api.codeMap).mockResolvedValue(mapOf(9) as never);
    renderDashboard();

    await screen.findByText("Items", { selector: "span" });
    expect(screen.queryByText("Nothing to summarise yet")).not.toBeInTheDocument();
    expect(screen.getByText("Graph nodes").parentElement).toHaveTextContent("9");
  });

  it("still draws the home when only an agent is online", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.dashboard).mockResolvedValue(bare as never);
    await zeroTheRest();
    vi.mocked(api.counts).mockResolvedValue({ ...countsZero, live_agents: 1 } as never);
    renderDashboard();

    await screen.findByText("Items", { selector: "span" });
    expect(screen.queryByText("Nothing to summarise yet")).not.toBeInTheDocument();
    expect(screen.getByText("Agents live").parentElement).toHaveTextContent("1");
  });

  it("names a failed fetch instead of reporting an empty project", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.dashboard).mockRejectedValue(new Error("boom"));
    renderDashboard();

    expect(await screen.findByText(/the request failed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    // The empty title is the wrong answer. Labels stay, with — where the dashboard
    // was the only source, and never a fabricated 0. The sibling reads succeeded
    // with non-zero values, so a real zero must not sneak in either.
    expect(screen.queryByText("Nothing to summarise yet")).not.toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(6);
    expect(screen.getByText("Memory shards")).toBeInTheDocument();
    expect(screen.queryByText("not pushed")).not.toBeInTheDocument();
    expect(screen.queryByText(/No deployment has pushed a code graph/)).not.toBeInTheDocument();
  });
});
