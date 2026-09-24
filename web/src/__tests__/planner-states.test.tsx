// @ts-nocheck — reads Node fs at runtime for sabotage guards.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import appFrameSrc from "../components/shell/AppFrame.tsx?raw";
import statusMenuSrc from "../features/tracker/StatusMenu.tsx?raw";
import itemRowSrc from "../features/tracker/ItemRow.tsx?raw";
import trackerSrc from "../features/tracker/TrackerView.tsx?raw";
import { ProjectProvider } from "@/features/ProjectContext";
import { PrdListView } from "@/features/prds/PrdListView";
import { TrackerView } from "@/features/tracker/TrackerView";
import type { Item, PrdSummary } from "@/lib/types";

const project = {
  id: "core",
  tag: "CORE",
  name: "Core",
  accent: "#a78bfa",
  visibility: "private",
  description: "",
  share_global_memory: false,
  auto_extract: true,
  mcp_enabled: true,
  embed_model: "",
};

const items: Item[] = [
  {
    id: "AL-01",
    project_id: "core",
    title: "First item",
    description: "",
    status: "in_progress",
    tags: [],
    touchpoints: [],
    effort: 3,
    sort_order: 0,
    blocker: "",
    bounce_reason: "",
    date: "Sep 23",
    reporter: { name: "Alex", handle: "alex", avatar: "#a78bfa" },
    pr: null,
    github_url: "",
    evidence: [],
    assignee: "",
    claimed_by: null,
    prd_id: null,
    prd_section: "",
    fidelity: "low",
    reach: "repo",
    created_at: "",
    updated_at: "",
  },
  {
    id: "AL-02",
    project_id: "core",
    title: "Second item",
    description: "",
    status: "done",
    tags: [],
    touchpoints: [],
    effort: 1,
    sort_order: 1,
    blocker: "",
    bounce_reason: "",
    date: "Sep 22",
    reporter: { name: "Alex", handle: "alex", avatar: "#a78bfa" },
    pr: null,
    github_url: "",
    evidence: [],
    assignee: "",
    claimed_by: null,
    prd_id: null,
    prd_section: "",
    fidelity: "low",
    reach: "repo",
    created_at: "",
    updated_at: "",
  },
];

let servedItems: Item[] = items;
let servedPrds: PrdSummary[] = [];
let itemsLoading = false;
let itemsError = false;

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [project]),
    config: vi.fn(async () => ({ hosted_mode: false })),
    items: vi.fn(async () => {
      if (itemsError) throw new Error("items failed");
      return servedItems;
    }),
    prds: vi.fn(async () => servedPrds),
    shards: vi.fn(async () => []),
    links: vi.fn(async () => []),
    updateItem: vi.fn(async (id: string, body: Partial<Item>) => ({ ...items[0], id, ...body })),
    reorderItems: vi.fn(async () => servedItems),
    assistantProviders: vi.fn(async () => ({ providers: [] })),
    assistantThreads: vi.fn(async () => []),
    createPrd: vi.fn(async () => ({ id: "PRD-NEW" })),
  },
}));

vi.mock("@/lib/queries", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/queries")>();
  return {
    ...actual,
    useItems: () => ({
      data: servedItems,
      isLoading: itemsLoading,
      isError: itemsError,
      refetch: vi.fn(),
    }),
    usePrds: () => ({
      data: servedPrds,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    }),
  };
});

function renderTracker() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/tracker"]}>
        <ProjectProvider>
          <Routes>
            <Route path="/tracker" element={<TrackerView />} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("planner surfaces (GRPH-913)", () => {
  beforeEach(() => {
    servedItems = items;
    servedPrds = [];
    itemsLoading = false;
    itemsError = false;
    Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {});
  });

  it("shows invitation copy when the project has zero items", async () => {
    servedItems = [];
    renderTracker();
    expect(await screen.findByText("No items yet")).toBeInTheDocument();
    expect(screen.queryByText("No items match.")).not.toBeInTheDocument();
  });

  it("shows filtered-empty copy with clear control when a filter hides all rows", async () => {
    const user = userEvent.setup();
    renderTracker();
    await screen.findByText("First item");
    await user.click(screen.getByRole("button", { name: /^Backlog/ }));
    expect(await screen.findByText(/No items match this filter/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filter" })).toBeInTheDocument();
  });

  it("shows a loading skeleton instead of centred Loading text", () => {
    itemsLoading = true;
    renderTracker();
    expect(screen.getByLabelText("Loading tracker")).toBeInTheDocument();
    expect(screen.queryByText(/Loading stream/)).not.toBeInTheDocument();
  });

  it("sabotage: reverting empty copy to No items match would fail", () => {
    expect(trackerSrc).toMatch(/No items yet/);
    expect(trackerSrc).not.toMatch(/No items match\./);
  });
});

describe("PRD list empty (GRPH-913)", () => {
  beforeEach(() => {
    servedPrds = [];
  });

  it("shows invitation when there are no PRDs", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProjectProvider>
            <PrdListView />
          </ProjectProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("No PRDs yet")).toBeInTheDocument();
  });
});

describe("accessibility floor (GRPH-915)", () => {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");

  it("skip link targets main content", () => {
    expect(appFrameSrc).toMatch(/Skip to main content/);
    expect(appFrameSrc).toMatch(/id="main-content"/);
  });

  it("status menu trigger has an accessible name", () => {
    expect(statusMenuSrc).toMatch(/aria-label=\{`Status: \$\{meta\.label\}`\}/);
  });

  it("tracker exposes Move up and Move down actions", () => {
    expect(itemRowSrc).toMatch(/Move up/);
    expect(itemRowSrc).toMatch(/Move down/);
    expect(trackerSrc).toMatch(/ArrowUp/);
  });

  it("sabotage: removing skip link would fail the contract", () => {
    expect(appFrameSrc).toMatch(/Skip to main content/);
  });
});
