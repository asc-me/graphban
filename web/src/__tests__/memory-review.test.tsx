import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MemoryReviewView } from "@/features/memory/MemoryReviewView";
import { ProjectProvider } from "@/features/ProjectContext";
import type { Shard } from "@/lib/types";

const candidate: Shard = {
  id: "m9", text: "Agent guess: batch writes for perf.", scope: "item", source: "lesson from AL-12",
  status: "candidate", origin: "agent:loop-agent", item_id: "AL-12", project_id: "core",
  fresh: true, scoring_source: "", auto_confidence: null, created_at: "",
};

// An auto-rejected near-duplicate — the "recent auto-actions" lane (AL-227).
const autoRejected: Shard = {
  id: "m10", text: "Duplicate: batch writes for perf.", scope: "item", source: "",
  status: "rejected", origin: "agent:loop-agent", item_id: "AL-12", project_id: "core",
  fresh: true, scoring_source: "similarity", auto_confidence: 0.97, created_at: "",
};

// Published with NO human involved (AL-280 trusted / AL-282 agent). Once agents run the
// loop these are the reviewer's actual job, so they get their own label and filter.
const unvetted: Shard = {
  id: "m11", text: "Published by the agent while you were away.", scope: "global", source: "",
  status: "published", origin: "agent:loop-agent", item_id: null, project_id: "core",
  fresh: true, scoring_source: "agent", auto_confidence: 0.93, created_at: "",
};

// Hoisted so the (hoisted) vi.mock factory can reference the spies eagerly.
const { publishSpy, undoSpy } = vi.hoisted(() => ({
  publishSpy: vi.fn(async () => ({})),
  undoSpy: vi.fn(async () => ({})),
}));

const project = {
  id: "core", name: "Core", accent: "#a78bfa", visibility: "private", description: "",
  share_global_memory: false, auto_extract: true, mcp_enabled: true, embed_model: "",
  memory_auto_reject: true, memory_write_mode: "review", memory_llm_judge: false, agent_adjudication: false, allow_self_review: false,
};

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [project]),
    candidateShards: vi.fn(async () => [candidate]),
    candidateClusters: vi.fn(async () => []),
    scoredCandidates: vi.fn(async () => []),
    autoActions: vi.fn(async () => [autoRejected, unvetted]),
    publishShard: publishSpy,
    rejectShard: vi.fn(async () => ({ ...candidate, status: "rejected" })),
    promoteCluster: vi.fn(async () => ({ published: "", rejected: [] })),
    undoAutoShard: undoSpy,
  },
}));

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ProjectProvider>
        <MemoryReviewView />
      </ProjectProvider>
    </QueryClientProvider>,
  );
}

describe("Memory review queue", () => {
  it("shows candidates and publishes one", async () => {
    const user = userEvent.setup();
    renderView();

    expect(await screen.findByText(/Agent guess: batch writes/)).toBeInTheDocument();
    expect(screen.getByText("agent:loop-agent")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Publish/ }));
    expect(publishSpy).toHaveBeenCalledWith("m9");
  });

  it("shows the recent auto-actions lane and undoes one (AL-227)", async () => {
    const user = userEvent.setup();
    renderView();

    expect(await screen.findByText(/Recent auto-actions/)).toBeInTheDocument();
    expect(screen.getByText("auto-rejected")).toBeInTheDocument();
    expect(screen.getByText("97%")).toBeInTheDocument();

    // Scope to THIS shard's row: the lane now also carries unvetted publishes (AL-287),
    // so a bare getByRole(/Undo/) matches more than one button.
    const row = screen.getByText(/Duplicate: batch writes/).closest("div")!;
    await user.click(within(row).getByRole("button", { name: /Undo/ }));
    expect(undoSpy).toHaveBeenCalledWith("m10");
  });
});


describe("unreviewed shards (AL-287)", () => {
  it("labels a shard nobody reviewed differently from a scorer decision", async () => {
    renderView();
    // The scorer's own decision and an unvetted publish must not read the same — the
    // whole point is telling apart "the scorer was confident" from "nobody looked".
    expect(await screen.findByText("agent + judge")).toBeInTheDocument();
    expect(screen.getByText("similarity")).toBeInTheDocument();
  });

  it("filters to only what nobody reviewed, in one click", async () => {
    renderView();
    const toggle = await screen.findByRole("button", { name: /nobody reviewed/i });
    expect(screen.getByText(/Duplicate: batch writes/)).toBeInTheDocument();

    await userEvent.click(toggle);
    expect(screen.getByText(/Published by the agent while you were away/)).toBeInTheDocument();
    expect(screen.queryByText(/Duplicate: batch writes/)).not.toBeInTheDocument();
  });

  it("counts unreviewed shards in the header, not just candidates", async () => {
    renderView();
    expect(await screen.findByText("1 UNREVIEWED")).toBeInTheDocument();
  });
});
