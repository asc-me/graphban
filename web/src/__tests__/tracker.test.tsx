import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { TrackerView } from "@/features/tracker/TrackerView";
import { CHECK_COLOR, PR_STATE_COLOR } from "@/lib/meta";
import type { Item } from "@/lib/types";

const items: Item[] = [
  {
    id: "AL-01", project_id: "core", title: "In progress thing", description: "",
    status: "in_progress", tags: ["ai"], touchpoints: [], effort: 5, sort_order: 0, blocker: "", bounce_reason: "", date: "Jul 19",
    reporter: { name: "Alex Cain", handle: "ascme", avatar: "#a78bfa" }, pr: null, github_url: "", evidence: [], assignee: "", claimed_by: null, prd_id: null, prd_section: "", fidelity: "low", reach: "repo",
    created_at: "", updated_at: "",
  },
  {
    id: "AL-02", project_id: "core", title: "Finished thing", description: "",
    status: "done", tags: ["ui"], touchpoints: [], effort: 8, sort_order: 1, blocker: "", bounce_reason: "", date: "Jul 14",
    reporter: { name: "Dana Ruiz", handle: "dev_ren", avatar: "#7ca2ff" }, pr: null, github_url: "", evidence: [], assignee: "", claimed_by: null, prd_id: null, prd_section: "", fidelity: "low", reach: "repo",
    created_at: "", updated_at: "",
  },
];

// What `api.items` serves. Defaults to the fixture; a test may swap it.
let served: Item[] = items;

const project = {
  id: "core", name: "Core", accent: "#a78bfa", visibility: "private", description: "",
  share_global_memory: false, auto_extract: true, mcp_enabled: true, embed_model: "",
};

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [project]),
    items: vi.fn(async () => served),
    shards: vi.fn(async () => []),
    updateItem: vi.fn(async (id: string, body: Partial<Item>) => ({ ...items[0], id, ...body })),
    reorderItems: vi.fn(async () => items),
    // Opening the detail panel mounts the assistant, which asks for these on mount.
    assistantProviders: vi.fn(async () => ({ providers: [] })),
    assistantThreads: vi.fn(async () => []),
  },
}));

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
    await user.click(screen.getByRole("button", { name: /^All/ }));
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
    const statusBtn = within(row).getByRole("button", { name: /Status:/ });
    statusBtn.focus();
    await user.keyboard("{Enter}");
    const reviewItem = await screen.findByRole("menuitem", { name: /Review/ });
    await user.click(reviewItem);

    await waitFor(() => expect(api.updateItem).toHaveBeenCalledWith("AL-01", { status: "review" }));
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
