import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { TrackerView } from "@/features/tracker/TrackerView";
import type { Item } from "@/lib/types";

const project = {
  id: "core", name: "Graphban", accent: "#a78bfa", visibility: "private", description: "",
  share_global_memory: false, auto_extract: true, mcp_enabled: true, embed_model: "",
};

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [project]),
    items: vi.fn(async (): Promise<Item[]> => []),
    shards: vi.fn(async () => []),
    updateItem: vi.fn(async () => ({})),
    reorderItems: vi.fn(async () => []),
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

describe("PlaceHeader", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows the view name in the Tracker (GRPH-941)", async () => {
    renderTracker();

    const header = await screen.findByTestId("place-header");
    expect(header).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Tracker");
  });

  it("renders the purpose line", async () => {
    renderTracker();

    await screen.findByTestId("place-header");
    expect(screen.getByText(/One linear stream/)).toBeInTheDocument();
  });

  it("renders the primary action when provided", async () => {
    renderTracker();

    await screen.findByTestId("place-header");
    // The NewItemDialog button is rendered inside the place header's action slot.
    const header = screen.getByTestId("place-header");
    expect(header.querySelector("button")).toBeInTheDocument();
  });

  it("sabotage: the place header element is present on the Tracker", async () => {
    renderTracker();

    // The place header must be present — removing it is the sabotage this test guards.
    const header = await screen.findByTestId("place-header");
    expect(header).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Tracker");
  });
});
