import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { TRACKER_COLUMNS_KEY } from "@/features/tracker/columnPrefs";
import { TRACKER_LAYOUT_KEY, TrackerView } from "@/features/tracker/TrackerView";
import { CHECK_COLOR, PR_STATE_COLOR } from "@/lib/meta";
import type { GraphLink, Item } from "@/lib/types";

const items: Item[] = [
  {
    id: "AL-01", project_id: "core", title: "In progress thing", description: "",
    status: "in_progress", tags: ["ai"], touchpoints: [], effort: 5, sort_order: 0, blocker: "", bounce_reason: "", date: "Jul 19",
    reporter: { name: "Alex Cain", handle: "ascme", avatar: "#a78bfa" }, pr: null, github_url: "", evidence: [], assignee: "", claimed_by: null, prd_id: null, prd_section: "", fidelity: "low", reach: "repo",
    created_at: "", updated_at: "", done_at: null,
  },
  {
    id: "AL-02", project_id: "core", title: "Finished thing", description: "",
    status: "done", tags: ["ui"], touchpoints: [], effort: 8, sort_order: 1, blocker: "", bounce_reason: "", date: "Jul 14",
    reporter: { name: "Dana Ruiz", handle: "dev_ren", avatar: "#7ca2ff" }, pr: null, github_url: "", evidence: [], assignee: "", claimed_by: null, prd_id: null, prd_section: "", fidelity: "low", reach: "repo",
    created_at: "", updated_at: "", done_at: "2026-07-14T12:00:00Z",
  },
];

// What `api.items` serves. Defaults to the fixture; a test may swap it.
let served: Item[] = items;
// What `api.links` serves. None by default.
let servedLinks: GraphLink[] = [];

const project = {
  id: "core", name: "Core", accent: "#a78bfa", visibility: "private", description: "",
  share_global_memory: false, auto_extract: true, mcp_enabled: true, embed_model: "",
};

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [project]),
    me: vi.fn(async () => ({ name: "Alex Cain" })),
    items: vi.fn(async () => served),
    shards: vi.fn(async () => []),
    links: vi.fn(async () => servedLinks),
    updateItem: vi.fn(async (id: string, body: Partial<Item>) => ({ ...items[0], id, ...body })),
    ownerClose: vi.fn(async (id: string) => ({ ...items[0], id, status: "done" as const })),
    reorderItems: vi.fn(async () => items),
    // Opening the detail panel mounts the assistant, which asks for these on mount.
    assistantProviders: vi.fn(async () => ({ providers: [] })),
    assistantThreads: vi.fn(async () => []),
  },
}));

// The suites below exercise the List layout; Board is the default and has its own suite.
// A saved default of "In flight" keeps the GRPH-918 first paint; the Current preset would
// also hide these fixtures' long-finished done item.
const setDefaultView = (id: string) => localStorage.setItem("gb_views_tracker", JSON.stringify({ saved: [], defaultId: id }));
beforeEach(() => {
  localStorage.setItem(TRACKER_LAYOUT_KEY, "list");
  setDefaultView("active");
});

function renderTracker() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/tracker"]}>
        <ProjectProvider>
          <Routes>
            <Route element={<Outlet context={""} />}>
              <Route path="/tracker" element={<TrackerView />} />
            </Route>
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("TrackerView", () => {
  beforeEach(() => vi.clearAllMocks());

  it("defaults to Active so done items are not first paint (GRPH-918)", async () => {
    renderTracker();
    expect(await screen.findByText("In progress thing")).toBeInTheDocument();
    expect(screen.queryByText("Finished thing")).not.toBeInTheDocument();
  });

  it("shows the full stream when All is selected", async () => {
    const user = userEvent.setup();
    renderTracker();
    await screen.findByText("In progress thing");
    await user.click(screen.getByRole("button", { name: /^All\s*\d+$/ }));
    expect(screen.getByText("Finished thing")).toBeInTheDocument();
  });

  it("includes blocked items in the Active default (GRPH-918)", async () => {
    served = [
      ...items,
      {
        ...items[0],
        id: "AL-04",
        title: "Blocked thing",
        status: "blocked",
        sort_order: 2,
      },
    ];
    try {
      renderTracker();
      expect(await screen.findByText("In progress thing")).toBeInTheDocument();
      expect(screen.getByText("Blocked thing")).toBeInTheDocument();
      expect(screen.queryByText("Finished thing")).not.toBeInTheDocument();
    } finally {
      served = items;
    }
  });

  it("filters by status", async () => {
    const user = userEvent.setup();
    renderTracker();
    await screen.findByText("In progress thing");

    // The "Done" filter chip narrows the stream to done items only.
    await user.click(screen.getByRole("button", { name: /^Done/ }));
    expect(screen.queryByText("In progress thing")).not.toBeInTheDocument();
    expect(screen.getByText("Finished thing")).toBeInTheDocument();
  });

  it("changes an item status via the row status menu", async () => {
    const user = userEvent.setup();
    const { api } = await import("@/lib/api");
    renderTracker();
    const row = (await screen.findByText("In progress thing")).closest("div")!;

    // Open the compact status menu on the row and pick "Review".
    const statusBtn = within(row).getByRole("button", { name: /Actions for/ });
    statusBtn.focus();
    await user.keyboard("{Enter}");
    const reviewItem = await screen.findByRole("menuitem", { name: /Set Review/ });
    await user.click(reviewItem);

    await waitFor(() => expect(api.updateItem).toHaveBeenCalledWith("AL-01", { status: "review" }));
  });

  it("advances status when the ring is clicked (GRPH-1169)", async () => {
    const user = userEvent.setup();
    const { api } = await import("@/lib/api");
    renderTracker();
    const row = (await screen.findByText("In progress thing")).closest("div")!;
    const ring = within(row).getByRole("button", { name: "Status: In Progress" });
    expect(ring).toHaveAttribute("title", "In Progress — click to advance");
    await user.click(ring);
    await waitFor(() => expect(api.updateItem).toHaveBeenCalledWith("AL-01", { status: "review" }));
  });

  it("keeps list rows at the design height and shows unassigned (GRPH-1169)", async () => {
    renderTracker();
    const row = (await screen.findByText("In progress thing")).closest("[aria-selected]")!;
    expect(row.className).toMatch(/h-\[45px\]/);
    expect(within(row as HTMLElement).getByText("unassigned")).toBeInTheDocument();
  });

  it("does not PATCH done on an unattested item — opens owner-close (GRPH-1030)", async () => {
    const user = userEvent.setup();
    const { api } = await import("@/lib/api");
    renderTracker();
    const row = (await screen.findByText("In progress thing")).closest("div")!;
    const statusBtn = within(row).getByRole("button", { name: /Actions for/ });
    statusBtn.focus();
    await user.keyboard("{Enter}");
    await user.click(await screen.findByRole("menuitem", { name: /Set Done/ }));

    expect(await screen.findByRole("heading", { name: /Close as merged/ })).toBeInTheDocument();
    expect(api.updateItem).not.toHaveBeenCalledWith("AL-01", { status: "done" });
  });

  it("still PATCHes done on a typed wait (GRPH-1030)", async () => {
    served = [{ ...items[0], id: "AL-W1", title: "Wait for merge", tags: ["wait:merge"],
                status: "blocked" }];
    try {
      const user = userEvent.setup();
      const { api } = await import("@/lib/api");
      renderTracker();
      const row = (await screen.findByText("Wait for merge")).closest("div")!;
      const statusBtn = within(row).getByRole("button", { name: /Actions for/ });
      statusBtn.focus();
      await user.keyboard("{Enter}");
      await user.click(await screen.findByRole("menuitem", { name: /Set Done/ }));
      await waitFor(() => expect(api.updateItem).toHaveBeenCalledWith("AL-W1", { status: "done" }));
      expect(api.ownerClose).not.toHaveBeenCalled();
    } finally {
      served = items;
    }
  });

  it("submits owner-close with the merge commit (GRPH-1030)", async () => {
    const user = userEvent.setup();
    const { api } = await import("@/lib/api");
    renderTracker();
    const row = (await screen.findByText("In progress thing")).closest("div")!;
    const statusBtn = within(row).getByRole("button", { name: /Actions for/ });
    statusBtn.focus();
    await user.keyboard("{Enter}");
    await user.click(await screen.findByRole("menuitem", { name: /Set Done/ }));
    await screen.findByRole("heading", { name: /Close as merged/ });
    await user.type(screen.getByPlaceholderText(/sha that landed on the base/), "abcdef1");
    await user.click(screen.getByRole("button", { name: /^Close as merged$/ }));
    await waitFor(() =>
      expect(api.ownerClose).toHaveBeenCalledWith("AL-01", { commit: "abcdef1", pr_url: undefined }),
    );
  });
});

describe("a bounced item", () => {
  // jsdom has no layout, so the assistant's autoscroll finds no scrollTo on the node.
  beforeEach(() => {
    Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {});
  });

  it("shows why it came back", async () => {
    // GRPH-378: the reason was required of the reviewer and then discarded, so the board
    // showed an item that had silently returned from review with no account of itself.
    served = [{ ...items[0], id: "AL-03", title: "Sent back", status: "in_progress",
                bounce_reason: "no test covers the refusal path" }];
    try {
      renderTracker();
      await userEvent.click(await screen.findByText("Sent back"));

      expect(await screen.findByText(/no test covers the refusal path/)).toBeInTheDocument();
    } finally {
      served = items;
    }
  });
});

describe("a blocker in another project (GRPH-1063)", () => {
  beforeEach(() => {
    Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {});
  });
  afterEach(() => {
    servedLinks = [];
  });

  const HIDDEN = "(an item in a project you cannot see)";
  const dep = (b: string, b_status: GraphLink["b_status"]): GraphLink => ({
    id: Math.floor(Math.random() * 1e6), a: "AL-01", b, type: "dependency", confidence: 1,
    reason: "", b_status,
  });

  it("shows a blocker the viewer cannot read as a placeholder, still blocking", async () => {
    // Not in this project's item list and no status: the absence must read as blocked.
    servedLinks = [dep(HIDDEN, null), dep(HIDDEN, null)];
    renderTracker();
    await userEvent.click(await screen.findByText("In progress thing"));

    expect(await screen.findAllByText(HIDDEN)).toHaveLength(2);
    expect(screen.getByText(/Blocked by/)).toBeInTheDocument();
  });

  it("reads a finished far blocker's status off the link", async () => {
    servedLinks = [dep("GBC-12", "done"), dep("GBC-13", "in_progress")];
    renderTracker();
    await userEvent.click(await screen.findByText("In progress thing"));

    expect(await screen.findByText("GBC-13")).toBeInTheDocument();
    expect(screen.queryByText("GBC-12")).not.toBeInTheDocument();
    expect(screen.getByText(/Depends on 2 items · 1 done/)).toBeInTheDocument();
  });
});

/**
 * The row furniture PRD-47 §S3 names: the `PROTO` badge, tags, effort, the PR chip with
 * its state and check colours, and the owner chip with a claim dot.
 *
 * Each chip is optional, so every case asserts both that it appears when its condition
 * holds and that it does NOT appear when it doesn't. Only the second half catches the
 * defect worth catching here — a badge that renders unconditionally still "shows the
 * PROTO badge" on the item that has one.
 *
 * The colours are asserted against `PR_STATE_COLOR` / `CHECK_COLOR` rather than against
 * hex literals, and through a second PR whose state and checks both differ: what §S3
 * specifies is that the chip is coloured *from* those maps, so a hardcoded colour or a
 * swapped pair has to fail.
 */
describe("the tracker row (PRD-47 S3)", () => {
  const pr = {
    number: 42, title: "Plan surface", branch: "gb/al-09", state: "open",
    additions: 120, deletions: 8, checks: "passing", ago: "1h",
  };

  const dressed: Item = {
    ...items[0],
    id: "AL-09",
    title: "Prototype the plan surface",
    status: "in_progress",
    tags: ["design", "frontend"],
    effort: 13,
    fidelity: "high",
    claimed_by: "GRPH-A77",
    assignee: "",
    pr,
  };

  async function showRow(row: Item) {
    served = [row];
    renderTracker();
    await screen.findByText(row.title);
  }

  afterEach(() => {
    served = items;
  });

  it("badges a high-fidelity item as needing a prototype", async () => {
    await showRow(dressed);
    const badge = screen.getByText("proto");
    expect(badge.getAttribute("title")).toMatch(/prototype/i);
  });

  it("shows no PROTO badge on a low-fidelity item", async () => {
    await showRow({ ...dressed, fidelity: "low" });
    expect(screen.queryByText("proto")).not.toBeInTheDocument();
  });

  it("shows the tags and the effort estimate", async () => {
    await showRow(dressed);
    expect(screen.getByText("design")).toBeInTheDocument();
    expect(screen.getByText("frontend")).toBeInTheDocument();
    expect(screen.getByTitle("effort")).toHaveTextContent("13");
  });

  it("shows the PR chip with its number and the tooltip §S3 specifies", async () => {
    await showRow(dressed);
    const chip = screen.getByTitle("PR #42 · open · checks passing");
    expect(chip).toHaveTextContent("#42");
    expect(chip).toHaveStyle({ color: PR_STATE_COLOR.open });
    expect(chip.querySelector(".rounded-full")).toHaveStyle({
      background: CHECK_COLOR.passing,
    });
  });

  it("colours the PR chip from its state and its checks, not from a constant", async () => {
    await showRow({ ...dressed, pr: { ...pr, state: "merged", checks: "failing" } });
    const chip = screen.getByTitle("PR #42 · merged · checks failing");
    expect(chip).toHaveStyle({ color: PR_STATE_COLOR.merged });
    expect(chip.querySelector(".rounded-full")).toHaveStyle({
      background: CHECK_COLOR.failing,
    });
  });

  it("shows no PR chip on an item without a PR", async () => {
    await showRow({ ...dressed, pr: null });
    expect(screen.queryByTitle(/^PR #/)).not.toBeInTheDocument();
  });

  it("shows the claimant with a live claim dot", async () => {
    await showRow(dressed);
    const owner = screen.getByTitle("Claimed by GRPH-A77");
    expect(owner).toHaveTextContent("GRPH-A77");
    expect(owner.querySelector(".blink")).toBeInTheDocument();
  });

  it("shows an assignee without a claim dot once nobody holds the item", async () => {
    await showRow({ ...dressed, claimed_by: null, assignee: "dana" });
    const owner = screen.getByTitle("Assigned to dana");
    expect(owner).toHaveTextContent("dana");
    // A claim dot asserts somebody is on it right now. An assignment does not.
    expect(owner.querySelector(".blink")).not.toBeInTheDocument();
    expect(screen.queryByTitle(/^Claimed by/)).not.toBeInTheDocument();
  });
});

/**
 * PRD GRPH-P60 §D3: Board is the default layout, a column drop sets status through the same
 * mutation StatusMenu uses, and a refused move snaps back with the server's reason.
 */
describe("the tracker board (GRPH-1144)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.removeItem(TRACKER_LAYOUT_KEY);
    setDefaultView("all");
  });
  afterEach(() => {
    served = items;
  });

  function column(name: string) {
    return screen.getByRole("region", { name });
  }

  function dragTo(card: HTMLElement, target: HTMLElement) {
    fireEvent.dragStart(card);
    fireEvent.dragOver(target);
    fireEvent.drop(target);
    fireEvent.dragEnd(card);
  }

  it("lands a fresh user on Board, with every column and done items in place", async () => {
    renderTracker();
    await screen.findByText("In progress thing");
    expect(screen.getByRole("button", { name: /Board/ })).toHaveAttribute("aria-pressed", "true");
    for (const label of ["Backlog", "Next", "In Progress", "Review", "Blocked", "Done"]) {
      expect(column(label)).toBeInTheDocument();
    }
    expect(within(column("Done")).getByText("Finished thing")).toBeInTheDocument();
    expect(within(column("Backlog")).getByText("Drop here")).toBeInTheDocument();
    expect(within(column("In Progress")).queryByText("Drop here")).not.toBeInTheDocument();
  });

  it("persists a switch to List across a reload", async () => {
    const user = userEvent.setup();
    const first = renderTracker();
    await screen.findByText("In progress thing");
    await user.click(screen.getByRole("button", { name: /List/ }));
    expect(screen.queryByRole("region", { name: "Backlog" })).not.toBeInTheDocument();
    first.unmount();

    renderTracker();
    await screen.findByText("In progress thing");
    expect(screen.getByRole("button", { name: /List/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("region", { name: "Backlog" })).not.toBeInTheDocument();
  });

  it("sets status when a card is dropped on another column", async () => {
    const { api } = await import("@/lib/api");
    renderTracker();
    await screen.findByText("In progress thing");
    dragTo(screen.getByTestId("card-AL-01"), column("Review"));
    await waitFor(() => expect(api.updateItem).toHaveBeenCalledWith("AL-01", { status: "review" }));
  });

  it("returns a refused card to its column and shows the reason (409)", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.updateItem).mockRejectedValueOnce(
      new Error(JSON.stringify({ detail: "GRPH-7 is still waiting on a dependency" })),
    );
    renderTracker();
    await screen.findByText("In progress thing");
    dragTo(screen.getByTestId("card-AL-01"), column("Review"));

    expect(await screen.findByRole("alert")).toHaveTextContent("GRPH-7 is still waiting on a dependency");
    await waitFor(() =>
      expect(within(column("In Progress")).getByText("In progress thing")).toBeInTheDocument(),
    );
    expect(within(column("Review")).queryByText("In progress thing")).not.toBeInTheDocument();
  });

  it("routes a drop on Done for an unattested item through owner-close", async () => {
    const { api } = await import("@/lib/api");
    renderTracker();
    await screen.findByText("In progress thing");
    dragTo(screen.getByTestId("card-AL-01"), column("Done"));
    expect(await screen.findByRole("heading", { name: /Close as merged/ })).toBeInTheDocument();
    expect(api.updateItem).not.toHaveBeenCalled();
  });

  it("dresses a card from the item: PROTO only when high fidelity, PR from item.pr", async () => {
    const pr = {
      number: 42, title: "", branch: "", state: "merged", additions: 0, deletions: 0,
      checks: "failing", ago: "",
    };
    served = [
      { ...items[0], id: "AL-09", fidelity: "high", claimed_by: "GRPH-A77", pr },
      { ...items[1], id: "AL-10", status: "next", fidelity: "low", assignee: "" },
    ];
    renderTracker();
    await screen.findByTestId("card-AL-09");

    const hi = screen.getByTestId("card-AL-09");
    const lo = screen.getByTestId("card-AL-10");
    expect(within(hi).getByText("PROTO")).toBeInTheDocument();
    expect(within(lo).queryByText("PROTO")).not.toBeInTheDocument();

    const chip = within(hi).getByTitle("PR #42 · merged · checks failing");
    expect(chip).toHaveTextContent("#42");
    expect(chip).toHaveStyle({ color: PR_STATE_COLOR.merged });
    expect(chip.querySelector(".rounded-full")).toHaveStyle({ background: CHECK_COLOR.failing });
    expect(within(lo).queryByTitle(/^PR #/)).not.toBeInTheDocument();

    expect(within(hi).getByTitle("Claimed by GRPH-A77").querySelector(".blink")).toBeInTheDocument();
    expect(within(lo).getByText("unassigned")).toBeInTheDocument();
  });

  it("shows the skeleton while loading and never the empty state", async () => {
    const { api } = await import("@/lib/api");
    let release: (v: Item[]) => void = () => {};
    vi.mocked(api.items).mockImplementationOnce(() => new Promise<Item[]>((r) => (release = r)));
    renderTracker();
    expect(await screen.findByLabelText("Loading tracker")).toBeInTheDocument();
    expect(screen.queryByText("No items yet")).not.toBeInTheDocument();
    release([]);
    expect(await screen.findByText("No items yet")).toBeInTheDocument();
    expect(screen.queryByLabelText("Loading tracker")).not.toBeInTheDocument();
  });

  it("shows the empty state with New item when there are no items", async () => {
    served = [];
    renderTracker();
    expect(await screen.findByText("No items yet")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /New item/i }).length).toBeGreaterThan(0);
    expect(screen.queryByRole("region", { name: "Backlog" })).not.toBeInTheDocument();
  });

  it("shows the error state when items fail to load", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.items).mockRejectedValueOnce(new Error("boom"));
    renderTracker();
    expect(await screen.findByText("Items unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByText("No items yet")).not.toBeInTheDocument();
  });
});

describe("board columns (GRPH-1145)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.removeItem(TRACKER_LAYOUT_KEY);
    localStorage.removeItem(TRACKER_COLUMNS_KEY);
    setDefaultView("all");
  });

  it("hides a column from the board but not from the list counts", async () => {
    const user = userEvent.setup();
    renderTracker();
    await screen.findByText("In progress thing");
    await user.click(screen.getByRole("button", { name: "Hide Done" }));
    expect(screen.queryByRole("region", { name: "Done" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Columns/ })).toHaveTextContent("1 hidden");

    await user.click(screen.getByRole("button", { name: /List/ }));
    expect(screen.getByRole("button", { name: /^Done/ })).toHaveTextContent("1");
  });

  it("collapses to a strip with the count, and clicking expands it", async () => {
    const user = userEvent.setup();
    renderTracker();
    await screen.findByText("In progress thing");
    await user.click(screen.getByRole("button", { name: "Collapse Done" }));
    expect(screen.queryByRole("region", { name: "Done" })).not.toBeInTheDocument();
    const strip = screen.getByRole("button", { name: "Expand Done (1)" });
    expect(strip).toHaveTextContent("1");
    await user.click(strip);
    expect(screen.getByRole("region", { name: "Done" })).toBeInTheDocument();
  });

  it("persists across a reload, summarises in the popover, and Reset restores all", async () => {
    const user = userEvent.setup();
    const first = renderTracker();
    await screen.findByText("In progress thing");
    await user.click(screen.getByRole("button", { name: "Hide Done" }));
    await user.click(screen.getByRole("button", { name: "Collapse Backlog" }));
    first.unmount();

    renderTracker();
    await screen.findByText("In progress thing");
    expect(screen.queryByRole("region", { name: "Done" })).not.toBeInTheDocument();
    const trigger = screen.getByRole("button", { name: "Columns 1 hidden · 1 collapsed" });
    await user.click(trigger);
    expect(await screen.findByText("Board columns")).toBeInTheDocument();
    expect(screen.getByText(/only you · AC/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reset" }));
    expect(screen.getByRole("region", { name: "Done" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Backlog" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Columns" })).toBeInTheDocument();
  });
});

describe("saved views, window and auto-hide (GRPH-1146)", () => {
  const daysAgo = (n: number) => new Date(Date.now() - n * 86_400_000).toISOString();
  const mk = (id: string, status: Item["status"], updated: string, doneAt: string | null): Item => ({
    ...items[0], id, title: `Thing ${id}`, status, updated_at: updated, created_at: updated, done_at: doneAt, sort_order: 0,
  });
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.removeItem("gb_views_tracker");
    localStorage.setItem(TRACKER_LAYOUT_KEY, "list");
    served = [
      mk("V-1", "in_progress", daysAgo(1), null),
      mk("V-2", "done", daysAgo(15), daysAgo(15)),
      mk("V-3", "done", daysAgo(2), daysAgo(2)),
    ];
  });
  afterEach(() => {
    served = items;
  });

  it("hides an item done 15 days ago, counts it in the footer, and shows it when back in review", async () => {
    renderTracker();
    expect(await screen.findByText("Thing V-1")).toBeInTheDocument();
    expect(screen.getByText("Thing V-3")).toBeInTheDocument();
    expect(screen.queryByText("Thing V-2")).not.toBeInTheDocument();
    expect(screen.getByText(/hidden by view: 1 done more than 14d ago/)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Current/ })).toHaveTextContent("2");

    served = [served[0], { ...served[1], status: "review", done_at: null }, served[2]];
    renderTracker();
    expect(await screen.findAllByText("Thing V-2")).not.toHaveLength(0);
  });

  it("tab counts equal the rows each view shows", async () => {
    renderTracker();
    await screen.findByText("Thing V-1");
    expect(screen.getByRole("tab", { name: /Current/ })).toHaveTextContent("2");
    expect(screen.getByRole("tab", { name: /Everything/ })).toHaveTextContent("3");
    expect(screen.getByRole("tab", { name: /In flight/ })).toHaveTextContent("1");
  });

  it("saves an edited view as a tab and keeps it and the default across a reload", async () => {
    const user = userEvent.setup();
    const first = renderTracker();
    await screen.findByText("Thing V-1");
    await user.click(screen.getByTitle("Time window and auto-hide"));
    await user.click(within(screen.getByRole("group", { name: "Auto-hide" })).getByRole("button", { name: "Never" }));
    expect(screen.getByText("EDITED")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save as view" }));
    const saved = screen.getByRole("tab", { name: /My view 1/ });
    expect(saved).toHaveAttribute("aria-selected", "true");
    await user.click(screen.getByRole("button", { name: /Set as default/ }));
    first.unmount();

    renderTracker();
    const tab = await screen.findByRole("tab", { name: /My view 1/ });
    expect(tab).toHaveAttribute("aria-selected", "true");
    expect(tab).toHaveTextContent("★");
  });

  it("shows the filtered-empty message, not the empty state, when the view hides everything", async () => {
    const user = userEvent.setup();
    served = [mk("V-2", "done", daysAgo(15), daysAgo(15))];
    renderTracker();
    expect(await screen.findByText(/No items in this view/)).toBeInTheDocument();
    expect(screen.queryByText("No items yet")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filter" }));
    expect(await screen.findByText("Thing V-2")).toBeInTheDocument();
  });
});
