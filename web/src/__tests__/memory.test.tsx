import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AgentSidebar } from "@/components/shell/AgentSidebar";
import { MemoryRouter } from "react-router-dom";

import { ProjectProvider } from "@/features/ProjectContext";
import type { ShardHit } from "@/lib/types";

const hits: ShardHit[] = [
  {
    shard: {
      id: "m1", text: "Decided: use pgvector to keep self-host to one Postgres container.",
      scope: "global", source: "from AL-08", status: "published", origin: "user:ascme",
      item_id: null, project_id: "core", fresh: false, scoring_source: "", auto_confidence: null,
      created_at: "",
    },
    score: 0.83,
  },
];

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    // A project must resolve for memory to have a scope — see the "without a project"
    // test below, which is the other half of this.
    projects: vi.fn(async () => [
      { id: "core", tag: "CORE", name: "Core", accent: "#c6f24e", visibility: "private",
        description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
        embed_model: "" },
    ]),
    shards: vi.fn(async () => []),
    searchMemory: vi.fn(async () => hits),
    addShard: vi.fn(async () => hits[0].shard),
    chat: vi.fn(async () => ({ reply: "ok", shards: [] })),
  },
}));

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/tracker"]}>
        <ProjectProvider>
        <AgentSidebar open onClose={() => {}} />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Memory panel", () => {
  beforeEach(() => vi.clearAllMocks());

  it("runs a semantic search and shows ranked results", async () => {
    const user = userEvent.setup();
    const { api } = await import("@/lib/api");
    renderSidebar();

    // Enabled only once a project resolves, so this is a find, not a get.
    const input = await screen.findByPlaceholderText(/Semantic search over memory/i);
    await user.type(input, "pgvector self-host{Enter}");

    expect(await screen.findByText(/keep self-host to one Postgres container/i)).toBeInTheDocument();
    expect(screen.getByText("0.83")).toBeInTheDocument();
    expect(api.searchMemory).toHaveBeenCalledWith("core", "pgvector self-host", 5);
  });

  it("refuses to search when no project has resolved", async () => {
    // The scope of a memory search is a project. Searching without one would come back
    // empty and read as "no matches", when the truth is that nothing was searched — so
    // the input says which of the two it is instead of quietly returning zero.
    const { api } = await import("@/lib/api");
    vi.mocked(api.projects).mockResolvedValueOnce([]);
    renderSidebar();

    const input = await screen.findByPlaceholderText(/No project selected/i);
    expect(input).toBeDisabled();
    expect(api.searchMemory).not.toHaveBeenCalled();
  });
});
