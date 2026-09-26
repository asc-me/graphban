import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
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

  it("shows the project name and view name in the Tracker (GRPH-941)", async () => {
    renderTracker();

    const header = await screen.findByTestId("place-header");
    expect(await within(header).findByText("Graphban")).toBeInTheDocument();
    expect(within(header).getByRole("heading", { level: 1 })).toHaveTextContent("Tracker");
  });

  it("renders the purpose line", async () => {
    renderTracker();

    const header = await screen.findByTestId("place-header");
    expect(within(header).getByText(/One linear stream/)).toBeInTheDocument();
  });

  it("renders the primary action when provided", async () => {
    renderTracker();

    const header = await screen.findByTestId("place-header");
    expect(header.querySelector("button")).toBeInTheDocument();
  });

  it("sabotage: dropping the project name from the header fails", async () => {
    renderTracker();

    const header = await screen.findByTestId("place-header");
    // Drop `{projectName}` from PlaceHeader — this assertion is the gate.
    expect(await within(header).findByText("Graphban")).toBeInTheDocument();
  });
});
