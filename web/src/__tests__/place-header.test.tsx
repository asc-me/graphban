import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { TrackerView } from "@/features/tracker/TrackerView";
import type { Item } from "@/lib/types";

const project = {
  id: "core", name: "Graphban", tag: "GRPH", accent: "#a78bfa", visibility: "private", description: "",
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
    const tag = await within(header).findByText("GRPH");
    const h1 = within(header).getByRole("heading", { level: 1 });
    expect(h1).toHaveTextContent("Tracker");
    expect(h1.parentElement).toContainElement(tag);
  });

  it("renders the purpose line", async () => {
    renderTracker();

    const header = await screen.findByTestId("place-header");
    // Board is the default layout (GRPH-1144); the purpose line is the same in both layouts (GRPH-1206).
    expect(within(header).getByText(/One linear stream\. Priority \+ recency/)).toBeInTheDocument();
  });

  it("renders the primary action when provided", async () => {
    renderTracker();

    const header = await screen.findByTestId("place-header");
    expect(header.querySelector("button")).toBeInTheDocument();
  });

  it("sabotage: dropping the project tag from the header fails", async () => {
    renderTracker();

    const header = await screen.findByTestId("place-header");
    // Drop `{projectTag}` from PlaceHeader — this assertion is the gate.
    expect(await within(header).findByText("GRPH")).toBeInTheDocument();
  });
});
