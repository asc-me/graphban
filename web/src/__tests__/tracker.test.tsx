import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { TrackerView } from "@/features/tracker/TrackerView";
import type { Item } from "@/lib/types";

const items: Item[] = [
  {
    id: "AL-01", project_id: "core", title: "In progress thing", description: "",
    status: "in_progress", tags: ["ai"], touchpoints: [], effort: 5, sort_order: 0, blocker: "", bounce_reason: "", date: "Jul 19",
    reporter: { name: "Alex Cain", handle: "ascme", avatar: "#a78bfa" }, pr: null, github_url: "", evidence: [], assignee: "", claimed_by: null, prd_id: null, prd_section: "", fidelity: "low",
    created_at: "", updated_at: "",
  },
  {
    id: "AL-02", project_id: "core", title: "Finished thing", description: "",
    status: "done", tags: ["ui"], touchpoints: [], effort: 8, sort_order: 1, blocker: "", bounce_reason: "", date: "Jul 14",
    reporter: { name: "Dana Ruiz", handle: "dev_ren", avatar: "#7ca2ff" }, pr: null, github_url: "", evidence: [], assignee: "", claimed_by: null, prd_id: null, prd_section: "", fidelity: "low",
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
      <ProjectProvider>
        <MemoryRouter initialEntries={["/tracker"]}>
          <Routes>
            <Route element={<Outlet context={""} />}>
              <Route path="/tracker" element={<TrackerView />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </ProjectProvider>
    </QueryClientProvider>,
  );
}

describe("TrackerView", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders the linear stream", async () => {
    renderTracker();
    expect(await screen.findByText("In progress thing")).toBeInTheDocument();
    expect(screen.getByText("Finished thing")).toBeInTheDocument();
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
    const statusBtn = within(row).getByRole("button");
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
    served = [{ ...items[0], id: "AL-03", title: "Sent back", status: "next",
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
