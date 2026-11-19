import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { TRACKER_LAYOUT_KEY, TrackerView } from "@/features/tracker/TrackerView";
import { putRefusalText } from "@/features/tracker/UnscopedFixes";
import { api } from "@/lib/api";
import type { ItemDetail } from "@/lib/types";

/** GRPH-1744: the maintenance line as the design draws it. */

const ACC = "Fix it.\n## Acceptance\n- it works";
const base = {
  project_id: "core", description: ACC, status: "next", tags: [], touchpoints: ["a.py"], effort: 1, sort_order: 0,
  blocker: "", bounce_reason: "", date: "", reporter: { name: "A B", handle: "ab", avatar: "#fff" }, pr: null,
  github_url: "", evidence: [], assignee: "", claimed_by: null, prd_id: null, prd_section: "", fidelity: "low",
  reach: "repo", created_at: "", updated_at: "", done_at: null,
} as const;
const item = (o: Partial<ItemDetail>): ItemDetail => ({ ...base, id: "AL-1", title: "t", ...o }) as ItemDetail;

let served: ItemDetail[] = [];
let line: { id: string; title: string } | null = null;
let unscopedIds: string[] = [];
/** What a PATCH does: by default it puts the item on the line and returns it. */
let patch: (id: string, body: Partial<ItemDetail>) => Promise<ItemDetail>;

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [{
      id: "core", name: "Core", accent: "#a78bfa", visibility: "private", description: "",
      share_global_memory: false, auto_extract: true, mcp_enabled: true, embed_model: "",
    }]),
    me: vi.fn(async () => ({ name: "A B" })),
    items: vi.fn(async () => served),
    itemsFull: vi.fn(async () => served),
    item: vi.fn(async (id: string) => served.find((i) => i.id === id)),
    shards: vi.fn(async () => []),
    links: vi.fn(async () => []),
    line: vi.fn(async () => line),
    unscopedFixes: vi.fn(async () => ({ ids: unscopedIds, count: unscopedIds.length })),
    createLine: vi.fn(async () => ({ id: "AL-L", title: "Maintenance" })),
    updateItem: vi.fn((id: string, body: Partial<ItemDetail>) => patch(id, body)),
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

const refusal = (detail: string) => new Error(JSON.stringify({ detail }));

beforeEach(() => {
  vi.clearAllMocks();
  // The sheet mounts the assistant, which scrolls its thread; jsdom has no scrollTo.
  Element.prototype.scrollTo = vi.fn() as unknown as typeof Element.prototype.scrollTo;
  localStorage.setItem(TRACKER_LAYOUT_KEY, "list");
  localStorage.setItem("gb_views_tracker", JSON.stringify({ saved: [], defaultId: "all" }));
  line = { id: "AL-L", title: "Maintenance" };
  unscopedIds = [];
  served = [item({ id: "AL-1", title: "Plain feature", kind: "feature" })];
  patch = async (id, body) => {
    // The server moves it: the next reads see it on the line and off the unscoped list.
    served = served.map((i) => (i.id === id ? { ...i, ...body } : i));
    unscopedIds = unscopedIds.filter((u) => u !== id);
    return served.find((i) => i.id === id)!;
  };
});

const maintChip = () => screen.findByRole("button", { name: /^Maintenance filter/ });

describe("the Maintenance chip", () => {
  it("comes after Response Needed and counts the items on the line", async () => {
    served = [
      item({ id: "AL-1", title: "Plain feature" }),
      item({ id: "AL-2", title: "Line fix", kind: "fix", line_id: "AL-L" }),
    ];
    renderTracker();
    const chip = await screen.findByRole("button", { name: "Maintenance filter, 1 item on the line" });
    const rn = screen.getByRole("button", { name: /^Response Needed/ });
    expect(rn.compareDocumentPosition(chip) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(chip).toHaveTextContent(/Maintenance\s*1/);
    expect(chip.className).not.toContain("border-dashed");
  });

  it("with no line has no count, a dashed border, and says the project has no line", async () => {
    line = null;
    renderTracker();
    const chip = await screen.findByRole("button", { name: "Maintenance filter, this project has no line" });
    expect(chip).toHaveTextContent(/^Maintenance$/);
    expect(chip.className).toContain("border-dashed");
  });
});

describe("the unscoped list", () => {
  it("reads 'N unscoped' and lists each fix with one action per row, and no bulk action", async () => {
    unscopedIds = ["AL-5", "AL-6"];
    served = [
      item({ id: "AL-1", title: "Plain feature" }),
      item({ id: "AL-5", title: "Webhook retries ignore the cap", kind: "fix" }),
      item({ id: "AL-6", title: "Search index skips archived", kind: "fix" }),
    ];
    const user = userEvent.setup();
    renderTracker();
    const trigger = await screen.findByTestId("unscoped-count");
    await waitFor(() => expect(trigger).toHaveTextContent("2 unscoped"));
    expect(trigger.className).toContain("text-[#e0b34a]");
    await user.click(trigger);
    const pop = screen.getByRole("dialog", { name: "Unscoped fixes" });
    expect(within(pop).getByText("2 unscoped")).toBeInTheDocument();
    expect(within(pop).getByText("Fixes with no PRD and no line. They are not in the Maintenance filter.")).toBeInTheDocument();
    expect(within(pop).getByTestId("unscoped-row-AL-5")).toHaveTextContent("Webhook retries ignore the cap");
    expect(within(pop).getByTestId("unscoped-row-AL-6")).toHaveTextContent("Search index skips archived");
    expect(within(pop).getAllByRole("button", { name: "Put on the maintenance line" })).toHaveLength(2);
    expect(within(pop).queryByRole("button", { name: /all/i })).not.toBeInTheDocument();
  });

  it("at 0 is muted and says there are none", async () => {
    const user = userEvent.setup();
    renderTracker();
    const trigger = await screen.findByTestId("unscoped-count");
    await waitFor(() => expect(trigger).toHaveTextContent("0 unscoped"));
    expect(trigger.className).not.toContain("text-[#e0b34a]");
    await user.click(trigger);
    expect(within(screen.getByRole("dialog", { name: "Unscoped fixes" })).getByText("No unscoped fixes.")).toBeInTheDocument();
  });

  it("puts one fix on the line: the row leaves, the done line names it, and both counts refresh", async () => {
    unscopedIds = ["AL-5", "AL-6"];
    served = [
      item({ id: "AL-1", title: "Plain feature" }),
      item({ id: "AL-5", title: "Webhook retries", kind: "fix" }),
      item({ id: "AL-6", title: "Search index", kind: "fix" }),
    ];
    const user = userEvent.setup();
    renderTracker();
    await screen.findByRole("button", { name: "Maintenance filter, 0 items on the line" });
    const trigger = await screen.findByTestId("unscoped-count");
    await waitFor(() => expect(trigger).toHaveTextContent("2 unscoped"));
    await user.click(trigger);
    const pop = screen.getByRole("dialog", { name: "Unscoped fixes" });
    await user.click(within(within(pop).getByTestId("unscoped-row-AL-5")).getByRole("button"));

    expect(api.updateItem).toHaveBeenCalledTimes(1);
    expect(api.updateItem).toHaveBeenCalledWith("AL-5", { line_id: "AL-L" });
    expect(await within(pop).findByRole("status")).toHaveTextContent("AL-5 is on the maintenance line.");
    expect(within(pop).queryByTestId("unscoped-row-AL-5")).not.toBeInTheDocument();
    expect(within(pop).getByTestId("unscoped-row-AL-6")).toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveTextContent("1 unscoped"));
    expect(await screen.findByRole("button", { name: "Maintenance filter, 1 item on the line" })).toBeInTheDocument();
  });

  it("on a refusal the row stays and says what the fix needs", async () => {
    unscopedIds = ["AL-5"];
    served = [item({ id: "AL-5", title: "Bare fix", kind: "fix", description: "", touchpoints: [] })];
    patch = async () => {
      throw refusal(
        "a fix needs an acceptance clause (a list item under `## Acceptance` in the description) and a " +
          "touchpoint (touchpoints=[...] naming the files the fix changes) before it can be filed",
      );
    };
    const user = userEvent.setup();
    renderTracker();
    const trigger = await screen.findByTestId("unscoped-count");
    await waitFor(() => expect(trigger).toHaveTextContent("1 unscoped"));
    await user.click(trigger);
    const pop = screen.getByRole("dialog", { name: "Unscoped fixes" });
    const row = within(pop).getByTestId("unscoped-row-AL-5");
    await user.click(within(row).getByRole("button", { name: "Put on the maintenance line" }));
    expect(await within(row).findByRole("alert")).toHaveTextContent(
      "AL-5 needs an acceptance clause and a file before it can go on the line.",
    );
    expect(within(pop).getByTestId("unscoped-row-AL-5")).toBeInTheDocument();
    expect(within(pop).queryByRole("status")).not.toBeInTheDocument();
    expect(trigger).toHaveTextContent("1 unscoped");
  });

  it("shows any other refusal as the server wrote it, on its row", async () => {
    unscopedIds = ["AL-5"];
    served = [item({ id: "AL-5", title: "Fix", kind: "fix" })];
    patch = async () => {
      throw refusal("only a fix can carry line_id (kind is 'feature')");
    };
    const user = userEvent.setup();
    renderTracker();
    const trigger = await screen.findByTestId("unscoped-count");
    await waitFor(() => expect(trigger).toHaveTextContent("1 unscoped"));
    await user.click(trigger);
    const row = within(screen.getByRole("dialog", { name: "Unscoped fixes" })).getByTestId("unscoped-row-AL-5");
    await user.click(within(row).getByRole("button"));
    expect(await within(row).findByRole("alert")).toHaveTextContent("only a fix can carry line_id (kind is 'feature')");
  });

  it("with no line refuses on the row and sends nothing", async () => {
    line = null;
    unscopedIds = ["AL-5"];
    served = [item({ id: "AL-5", title: "Fix", kind: "fix" })];
    const user = userEvent.setup();
    renderTracker();
    await screen.findByRole("button", { name: "Maintenance filter, this project has no line" });
    const trigger = await screen.findByTestId("unscoped-count");
    await waitFor(() => expect(trigger).toHaveTextContent("1 unscoped"));
    await user.click(trigger);
    const row = within(screen.getByRole("dialog", { name: "Unscoped fixes" })).getByTestId("unscoped-row-AL-5");
    await user.click(within(row).getByRole("button"));
    expect(within(row).getByRole("alert")).toHaveTextContent("This project has no maintenance line.");
    expect(api.updateItem).not.toHaveBeenCalled();
  });
});

describe("putRefusalText", () => {
  it("names only what is missing", () => {
    expect(putRefusalText("AL-9", refusal("a fix needs a touchpoint (touchpoints=[...]) before it can be filed"))).toBe(
      "AL-9 needs a file before it can go on the line.",
    );
    expect(
      putRefusalText("AL-9", refusal("a fix needs an acceptance clause (a list item) before it can be filed")),
    ).toBe("AL-9 needs an acceptance clause before it can go on the line.");
  });
});

describe("line cards and rows", () => {
  const lineItems = () => [
    item({ id: "AL-2", title: "Secure fix", kind: "fix", line_id: "AL-L", severity: "security", status: "next" }),
    item({ id: "AL-3", title: "Bare fix", kind: "fix", line_id: "AL-L", description: "", touchpoints: [], status: "backlog" }),
  ];

  it("draws SECURITY as the amber mono chip and readiness as a quiet line, in the list", async () => {
    served = lineItems();
    const user = userEvent.setup();
    renderTracker();
    await user.click(await maintChip());
    const sec = await screen.findByTestId("badge-security");
    expect(sec).toHaveTextContent("SECURITY");
    expect(sec.className).toContain("text-[#e0b34a]");
    expect(sec.className).toContain("font-mono");
    expect(sec.className).toContain("text-[9px]");
    expect(sec.className).not.toContain("text-st-blocked");
    const needs = await screen.findByTestId("line-needs");
    expect(needs).toHaveTextContent("needs acceptance · needs a file");
    expect(needs.className).toContain("text-muted");
    expect(needs.className).not.toContain("border");
  });

  it("keeps an unready item in its status column on the board; there is no Unready column", async () => {
    localStorage.setItem(TRACKER_LAYOUT_KEY, "board");
    served = lineItems();
    const user = userEvent.setup();
    renderTracker();
    await user.click(await maintChip());
    const backlog = await screen.findByRole("region", { name: "Backlog" });
    await waitFor(() => expect(within(backlog).getByTestId("line-needs")).toHaveTextContent("needs acceptance · needs a file"));
    expect(within(backlog).getByText("Bare fix")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /unready/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/^Unready$/i)).not.toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Review" })).getByText("None on the line here")).toBeInTheDocument();
  });
});

describe("the item sheet", () => {
  async function openSheet(title: string) {
    const user = userEvent.setup();
    renderTracker();
    await user.click(await screen.findByText(title));
    return screen.findByRole("dialog");
  }

  it("a line item says Maintenance · <line>, where it was found, and what it needs", async () => {
    served = [item({ id: "AL-3", title: "Bare fix", kind: "fix", line_id: "AL-L", description: "", touchpoints: [], found_during: "AL-1" })];
    const sheet = await openSheet("Bare fix");
    expect(within(sheet).getByTestId("item-scope")).toHaveTextContent("Maintenance · AL-L");
    expect(await within(sheet).findByTestId("found-during")).toHaveTextContent("Found during AL-1");
    const needs = within(sheet).getByTestId("sheet-needs");
    expect(needs).toHaveTextContent("needs acceptance");
    expect(needs).toHaveTextContent("needs a file");
    expect(needs).toHaveTextContent("The line drain will not claim this until both exist.");
  });

  it("a ready line fix with no provenance says 'not recorded' and lists no needs", async () => {
    served = [item({ id: "AL-2", title: "Ready fix", kind: "fix", line_id: "AL-L", found_during: null })];
    const sheet = await openSheet("Ready fix");
    expect(await within(sheet).findByTestId("found-during")).toHaveTextContent("Found during: not recorded");
    expect(within(sheet).queryByTestId("sheet-needs")).not.toBeInTheDocument();
    expect(within(sheet).queryByText("The line drain will not claim this until both exist.")).not.toBeInTheDocument();
  });

  it("a PRD item says Implements; an item with neither says No PRD, no line", async () => {
    served = [
      item({ id: "AL-4", title: "PRD work", prd_id: "AL-P1", kind: "feature" }),
      item({ id: "AL-5", title: "Loose work", kind: "feature" }),
    ];
    const user = userEvent.setup();
    renderTracker();
    await user.click(await screen.findByText("PRD work"));
    let sheet = await screen.findByRole("dialog");
    expect(within(sheet).getByTestId("item-scope")).toHaveTextContent("Implements AL-P1");
    expect(within(sheet).queryByTestId("found-during")).not.toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await user.click(screen.getByText("Loose work"));
    sheet = await screen.findByRole("dialog");
    const scope = within(sheet).getByTestId("item-scope");
    expect(scope).toHaveTextContent("No PRD, no line");
    expect(within(scope).getByText("No PRD, no line").className).toContain("text-[#e0b34a]");
  });
});
