import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AgentSidebar } from "@/components/shell/AgentSidebar";
import { MemoryRouter } from "react-router-dom";

import { ProjectProvider } from "@/features/ProjectContext";
import type { Item, Shard, ShardHit } from "@/lib/types";

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

const staleCooldown: Shard = {
  id: "m-cool",
  text: "PR linked 12s ago and the cooldown is 600s, so CI has not had time to run.",
  scope: "global",
  source: "gate refusal: pr_cooldown",
  status: "published",
  origin: "server",
  item_id: "GRPH-915",
  project_id: "core",
  fresh: false,
  scoring_source: "",
  auto_confidence: null,
  created_at: "",
};

const liveCooldown: Shard = {
  ...staleCooldown,
  id: "m-live",
  item_id: "GRPH-999",
  text: "Still in review — cooldown applies.",
};

const doneItem: Item = {
  id: "GRPH-915",
  project_id: "core",
  title: "Done gate item",
  description: "",
  status: "done",
  tags: [],
  touchpoints: [],
  effort: 1,
  sort_order: 0,
  blocker: "",
  bounce_reason: "",
  date: "",
  reporter: { name: "", handle: "", avatar: "" },
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
};

const reviewItem: Item = {
  ...doneItem,
  id: "GRPH-999",
  title: "In review",
  status: "review",
};

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
    shards: vi.fn(async () => [staleCooldown, liveCooldown]),
    items: vi.fn(async () => [doneItem, reviewItem]),
    counts: vi.fn(async () => ({ items: 2, items_in_progress: 0, requests: 0, review: 763 })),
    searchMemory: vi.fn(async () => hits),
    addShard: vi.fn(async () => hits[0].shard),
    chat: vi.fn(async () => ({ reply: "ok", shards: [] })),
  },
}));

function renderSidebar(path = "/tracker") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
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

  it("hides gate-refusal shards for done items but keeps live cooldown notes (GRPH-923)", async () => {
    renderSidebar();
    expect(await screen.findByText(/Still in review — cooldown applies/)).toBeInTheDocument();
    expect(screen.queryByText(/CI has not had time to run/)).not.toBeInTheDocument();
    expect(screen.getByText(/1 shards shown/i)).toBeInTheDocument();
  });

  it("names the memory-review queue count instead of silent zero (GRPH-923)", async () => {
    renderSidebar("/memory-review");
    expect(await screen.findByText(/763 waiting · 1 shards shown/i)).toBeInTheDocument();
  });
});
