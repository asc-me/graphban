import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { PrdListView } from "@/features/prds/PrdListView";
import type { PrdSummary } from "@/lib/types";

const daysAgo = (n: number) => new Date(Date.now() - n * 86_400_000).toISOString();
const prd = (id: string, status: PrdSummary["status"], updated: string, closed?: string): PrdSummary => ({
  id, title: `Spec ${id}`, status, version: "v1", linked: [], updated_at: updated, closed_at: closed ?? null,
});

let served: PrdSummary[] = [];

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [
      { id: "core", name: "Core", accent: "#a78bfa", visibility: "private", description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true, embed_model: "" },
    ]),
    me: vi.fn(async () => ({ name: "Alex Cain" })),
    prds: vi.fn(async () => served),
  },
}));

function renderPrds() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ProjectProvider>
          <PrdListView />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PRD list views (GRPH-1146)", () => {
  beforeEach(() => {
    localStorage.removeItem("gb_views_prds");
    served = [
      prd("P-1", "draft", daysAgo(3)),
      prd("P-2", "closed", daysAgo(20), daysAgo(20)),
      prd("P-3", "closed", daysAgo(4), daysAgo(4)),
    ];
  });

  it("hides a PRD closed more than 14 days ago using closed_at, and counts it", async () => {
    renderPrds();
    expect(await screen.findByText("Spec P-1")).toBeInTheDocument();
    expect(screen.getByText("Spec P-3")).toBeInTheDocument();
    expect(screen.queryByText("Spec P-2")).not.toBeInTheDocument();
    expect(screen.getByText(/hidden by view: 1 closed more than 14d ago/)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Current/ })).toHaveTextContent("2");
    expect(screen.getByRole("tab", { name: /Everything/ })).toHaveTextContent("3");
  });

  it("shows filtered-empty with Clear filter when the view hides every PRD", async () => {
    const user = userEvent.setup();
    served = [prd("P-2", "closed", daysAgo(20), daysAgo(20))];
    renderPrds();
    expect(await screen.findByText(/No PRDs in this view/)).toBeInTheDocument();
    expect(screen.queryByText("No PRDs yet")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filter" }));
    expect(await screen.findByText("Spec P-2")).toBeInTheDocument();
  });
});
