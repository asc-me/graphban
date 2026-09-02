import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MemoryReviewView } from "@/features/memory/MemoryReviewView";
import { MemoryRouter } from "react-router-dom";

import { ProjectProvider } from "@/features/ProjectContext";
import type { CandidateJudge, Shard } from "@/lib/types";

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
const { publishSpy, undoSpy, judgeSpy } = vi.hoisted(() => ({
  publishSpy: vi.fn(async () => ({})),
  undoSpy: vi.fn(async () => ({})),
  judgeSpy: vi.fn(async (id: string): Promise<CandidateJudge> => ({
    shard_id: id,
    verdict: null,
    cause: "no_provider",
    cause_detail: "no independent chat model is configured for this project",
  })),
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
    judgeShard: judgeSpy,
  },
}));

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/memory-review"]}>
        <ProjectProvider>
        <MemoryReviewView />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Memory review queue", () => {
  it("shows candidates and publishes one", async () => {
    const user = userEvent.setup();
    renderView();

    expect(await screen.findByText(/Agent guess: batch writes/)).toBeInTheDocument();
    expect(screen.getByText("agent:loop-agent")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Ask the judge/ })).not.toBeInTheDocument();

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


describe("review judge signals (GRPH-79)", () => {
  it("shows ungrounded and not-ready without looking like a publish", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.scoredCandidates).mockResolvedValueOnce([
      {
        shard: candidate,
        suggestion: "review",
        confidence: 0.4,
        reasons: ["review judge: contradicts published memory"],
        duplicate_of: null,
        judged: true,
        grounded: false,
        ready: false,
        conflicts: ["published m1: we never batch writes"],
        judge_reason: "contradicts published memory",
        ungraded_reason: "",
      },
    ]);
    renderView();
    expect(await screen.findByText("ungrounded")).toBeInTheDocument();
    expect(screen.getByText("not ready")).toBeInTheDocument();
    expect(screen.getByText(/Conflicts:/)).toBeInTheDocument();
    expect(screen.queryByText("not judged")).not.toBeInTheDocument();
  });

  it("names an ungraded judge rather than showing a quiet zero", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.scoredCandidates).mockResolvedValueOnce([
      {
        shard: candidate,
        suggestion: "review",
        confidence: 0.3,
        reasons: ["novel — no strong signal either way"],
        duplicate_of: null,
        judged: false,
        grounded: null,
        ready: null,
        conflicts: [],
        judge_reason: "",
        ungraded_reason: "stub cannot judge substance",
      },
    ]);
    renderView();
    expect(await screen.findByText(/not judged — stub cannot judge substance/)).toBeInTheDocument();
    expect(screen.queryByText("ungrounded")).not.toBeInTheDocument();
    expect(screen.queryByText("grounded")).not.toBeInTheDocument();
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


describe("on-demand LLM judge (GRPH-650)", () => {
  beforeEach(() => {
    project.memory_llm_judge = true;
    judgeSpy.mockClear();
  });
  afterEach(() => {
    project.memory_llm_judge = false;
  });

  it("does not show a judge score before anyone asks", async () => {
    renderView();
    expect(await screen.findByRole("button", { name: /Ask the judge/ })).toBeInTheDocument();
    expect(screen.queryByText(/^Judge:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Judge unavailable/)).not.toBeInTheDocument();
  });

  it("shows unavailable copy, not a quality number, when the judge cannot run", async () => {
    renderView();
    await userEvent.click(await screen.findByRole("button", { name: /Ask the judge/ }));
    expect(await screen.findByText(/Judge unavailable/)).toBeInTheDocument();
    expect(screen.queryByText(/Judge: 0%/)).not.toBeInTheDocument();
    expect(judgeSpy).toHaveBeenCalledWith("m9");
  });

  it("shows the verdict quality and reason when the judge answers", async () => {
    judgeSpy.mockResolvedValueOnce({
      shard_id: "m9",
      verdict: { keep: true, quality: 0.9, reason: "durable specific convention" },
      cause: null,
      cause_detail: "",
    });
    renderView();
    await userEvent.click(await screen.findByRole("button", { name: /Ask the judge/ }));
    expect(await screen.findByText(/Judge: 90%/)).toBeInTheDocument();
    expect(screen.getByText(/durable specific convention/)).toBeInTheDocument();
  });
});
