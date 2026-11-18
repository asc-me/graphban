import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { hasAcceptance, lineNeeds } from "@/features/tracker/LineBadges";
import { TRACKER_LAYOUT_KEY, TrackerView } from "@/features/tracker/TrackerView";
import type { GraphLink, Item, ItemDetail } from "@/lib/types";

const base = {
  project_id: "core", description: "", status: "next", tags: [], touchpoints: ["a.py"], effort: 1, sort_order: 0,
  blocker: "", bounce_reason: "", date: "", reporter: { name: "A B", handle: "ab", avatar: "#fff" }, pr: null,
  github_url: "", evidence: [], assignee: "", claimed_by: null, prd_id: null, prd_section: "", fidelity: "low",
  reach: "repo", created_at: "", updated_at: "", done_at: null,
} as const;

const item = (o: Partial<ItemDetail>): ItemDetail => ({ ...base, id: "AL-1", title: "t", ...o }) as ItemDetail;

let served: ItemDetail[] = [];
let line: { id: string; title: string } | null = null;
let unscoped: () => Promise<{ ids: string[]; count: number }> = async () => ({ ids: [], count: 0 });
let servedLinks: GraphLink[] = [];

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
    links: vi.fn(async () => servedLinks),
    line: vi.fn(async () => line),
    unscopedFixes: vi.fn(() => unscoped()),
    createLine: vi.fn(async () => ({ id: "AL-L", title: "Maintenance" })),
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

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.setItem(TRACKER_LAYOUT_KEY, "list");
  localStorage.setItem("gb_views_tracker", JSON.stringify({ saved: [], defaultId: "all" }));
  line = null;
  unscoped = async () => ({ ids: [], count: 0 });
  servedLinks = [];
  served = [item({ id: "AL-1", title: "Plain feature" })];
});

const chip = () => screen.findByRole("button", { name: /^Maintenance\s*\d+$/ });

describe("tracker Maintenance filter (GRPH-1701)", () => {
  it("shows only the line's items and leaves the status chips unselected", async () => {
    line = { id: "AL-L", title: "Maintenance" };
    served = [
      item({ id: "AL-1", title: "Plain feature" }),
      item({ id: "AL-2", title: "Line fix", kind: "fix", line_id: "AL-L", severity: "security" }),
    ];
    const user = userEvent.setup();
    renderTracker();
    await screen.findByText("Plain feature");
    await waitFor(() => expect(screen.getByRole("button", { name: /^Maintenance\s*1$/ })).toBeInTheDocument());
    await user.click(await chip());
    expect(screen.queryByText("Plain feature")).not.toBeInTheDocument();
    expect(screen.getByText("Line fix")).toBeInTheDocument();
    expect(screen.getByTestId("badge-fix")).toBeInTheDocument();
    expect(screen.getByTestId("badge-security")).toHaveTextContent("SECURITY");
    expect(screen.getByRole("button", { name: /^All\s*\d+$/ }).className).not.toBe(chipClass(await chip()));
  });

  it("keeps the board's columns when the filter is on (board layout)", async () => {
    localStorage.setItem(TRACKER_LAYOUT_KEY, "board");
    line = { id: "AL-L", title: "Maintenance" };
    served = [
      item({ id: "AL-1", title: "Plain feature" }),
      item({ id: "AL-2", title: "Line fix", kind: "fix", line_id: "AL-L", severity: "security" }),
    ];
    const user = userEvent.setup();
    renderTracker();
    await screen.findByText("Plain feature");
    await waitFor(() => expect(screen.getByRole("button", { name: /^Maintenance\s*1$/ })).toBeInTheDocument());
    await user.click(await chip());
    expect(await screen.findByText("Line fix")).toBeInTheDocument();
    expect(screen.queryByText("Plain feature")).not.toBeInTheDocument();
    expect(screen.getByTestId("badge-security")).toBeInTheDocument();
  });

  it("says the project has no line, and offers to create it", async () => {
    const user = userEvent.setup();
    renderTracker();
    await user.click(await chip());
    expect(await screen.findByText("This project has no maintenance line.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create the maintenance line" })).toBeInTheDocument();
    expect(screen.queryByText("The maintenance line has no items.")).not.toBeInTheDocument();
  });

  it("says a line with no items has none — a different sentence", async () => {
    line = { id: "AL-L", title: "Maintenance" };
    const user = userEvent.setup();
    renderTracker();
    await user.click(await chip());
    expect(await screen.findByText("The maintenance line has no items.")).toBeInTheDocument();
    expect(screen.queryByText("This project has no maintenance line.")).not.toBeInTheDocument();
  });

  it("shows the unscoped count beside the filter, 0 included", async () => {
    renderTracker();
    await screen.findByText("Plain feature");
    await waitFor(() => expect(screen.getByTestId("unscoped-count")).toHaveTextContent("Unscoped fixes0"));
  });

  it("shows an unscoped-count failure as an error, never as 0", async () => {
    unscoped = async () => {
      throw new Error("boom");
    };
    renderTracker();
    await screen.findByText("Plain feature");
    await waitFor(() => expect(screen.getByTestId("unscoped-count")).toHaveTextContent("could not load"));
    expect(screen.getByTestId("unscoped-count")).not.toHaveTextContent(/Unscoped fixes\s*0/);
  });

  it("names what an unready line item needs", async () => {
    line = { id: "AL-L", title: "Maintenance" };
    served = [item({ id: "AL-2", title: "Bare fix", kind: "fix", line_id: "AL-L", touchpoints: [] })];
    const user = userEvent.setup();
    renderTracker();
    await user.click(await chip());
    await waitFor(() => expect(screen.getByTestId("badge-needs")).toHaveTextContent("needs acceptance · needs a file"));
  });

  it("labels an item blocked by a fix, and the fix says what it blocks", async () => {
    line = { id: "AL-L", title: "Maintenance" };
    served = [
      item({ id: "AL-3", title: "Stuck feature", status: "blocked" }),
      item({ id: "AL-4", title: "The fix", kind: "fix", line_id: "AL-L" }),
    ];
    servedLinks = [{ type: "dependency", a: "AL-3", b: "AL-4", reason: "" } as GraphLink];
    renderTracker();
    await waitFor(() => expect(screen.getByTestId("badge-blocked-by")).toHaveTextContent("BLOCKED by AL-4"));
    expect(screen.getByTestId("badge-blocks")).toHaveTextContent("blocks AL-3");
  });
});

function chipClass(el: HTMLElement) {
  return el.className;
}

describe("lineNeeds", () => {
  const it0 = { line_id: "L", touchpoints: ["x"] } as Item;
  it("reads acceptance clauses", () => {
    expect(hasAcceptance("x\n## Acceptance\n- one")).toBe(true);
    expect(hasAcceptance("no clauses here")).toBe(false);
    expect(lineNeeds(it0, "nothing")).toEqual(["needs acceptance"]);
    expect(lineNeeds(it0, "## Acceptance\n- ok")).toEqual([]);
    expect(lineNeeds({ ...it0, touchpoints: [] } as Item, "## Acceptance\n- ok")).toEqual(["needs a file"]);
    expect(lineNeeds({ ...it0, line_id: null } as Item, "")).toEqual([]);
  });
});
