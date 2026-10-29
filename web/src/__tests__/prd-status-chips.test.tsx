import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { PrdListView } from "@/features/prds/PrdListView";
import type { PrdSummary } from "@/lib/types";

const now = new Date().toISOString();
const prd = (id: string, status: PrdSummary["status"]): PrdSummary => ({
  id, title: `Spec ${id}`, status, version: "v1", linked: [], updated_at: now, closed_at: null,
});
const served = [prd("P-1", "draft"), prd("P-2", "draft"), prd("P-3", "review"), prd("P-4", "approved")];

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

describe("PRD status chips (GRPH-1170)", () => {
  it("counts per status and filters the list on click", async () => {
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
    expect(await screen.findByText("Spec P-1")).toBeInTheDocument();
    const group = screen.getByRole("group", { name: "Filter by status" });
    const chip = (name: string) => Array.from(group.querySelectorAll("button")).find((b) => b.textContent?.startsWith(name))!;
    expect(chip("All")).toHaveTextContent("4");
    expect(chip("Draft")).toHaveTextContent("2");
    expect(chip("Review")).toHaveTextContent("1");
    expect(chip("Approved")).toHaveTextContent("1");
    expect(chip("Closed")).toHaveTextContent("0");

    await userEvent.click(chip("Draft"));
    expect(screen.getByText("Spec P-1")).toBeInTheDocument();
    expect(screen.getByText("Spec P-2")).toBeInTheDocument();
    expect(screen.queryByText("Spec P-3")).not.toBeInTheDocument();
    expect(screen.queryByText("Spec P-4")).not.toBeInTheDocument();
  });
});
