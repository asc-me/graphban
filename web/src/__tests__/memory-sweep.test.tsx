import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { MemoryReviewView } from "@/features/memory/MemoryReviewView";
import { ProjectProvider } from "@/features/ProjectContext";
import type { ScoredCandidate, Shard } from "@/lib/types";

const candidate: Shard = {
  id: "c1", text: "Use retry with exponential backoff for HTTP calls", scope: "item",
  source: "lesson from GRPH-12", status: "candidate", origin: "agent:planner",
  item_id: "GRPH-12", project_id: "core", fresh: true, scoring_source: "",
  auto_confidence: null, created_at: "2026-09-20T10:00:00Z",
};

const conflictCandidate: Shard = {
  id: "c2", text: "Never retry HTTP calls — fail fast instead", scope: "global",
  source: "", status: "candidate", origin: "agent:loop", item_id: null,
  project_id: "core", fresh: true, scoring_source: "", auto_confidence: null,
  created_at: "2026-09-21T10:00:00Z",
};

const dupeCandidate: Shard = {
  id: "c3", text: "Use retry with backoff for HTTP", scope: "item",
  source: "", status: "candidate", origin: "agent:planner", item_id: "GRPH-14",
  project_id: "core", fresh: true, scoring_source: "", auto_confidence: null,
  created_at: "2026-09-22T10:00:00Z",
};

const staleAuto: Shard = {
  id: "a1", text: "Auto-published: prefer composition over inheritance", scope: "global",
  source: "", status: "published", origin: "agent:scorer", item_id: null,
  project_id: "core", fresh: false, scoring_source: "llm", auto_confidence: 0.85,
  created_at: "2026-09-01T10:00:00Z",
};

const unvettedAuto: Shard = {
  id: "a2", text: "Auto-published by agent on write", scope: "global",
  source: "", status: "published", origin: "agent:loop", item_id: null,
  project_id: "core", fresh: true, scoring_source: "agent", auto_confidence: 0.9,
  created_at: "2026-09-25T10:00:00Z",
};

const lowConf: Shard = {
  id: "c4", text: "Weak signal: maybe use a cache layer", scope: "item",
  source: "", status: "candidate", origin: "agent:planner", item_id: "GRPH-20",
  project_id: "core", fresh: true, scoring_source: "", auto_confidence: null,
  created_at: "2026-09-23T10:00:00Z",
};

const scored: ScoredCandidate[] = [
  {
    shard: conflictCandidate, suggestion: "review", confidence: 0.5,
    reasons: ["contradicts published memory"], duplicate_of: null, judged: true,
    grounded: false, ready: false, conflicts: ["Published shard p1: fail fast always"],
    judge_reason: "contradicts published", ungraded_reason: "",
  },
  {
    shard: dupeCandidate, suggestion: "reject", confidence: 0.8,
    reasons: ["near-duplicate of c1"], duplicate_of: "c1", judged: true,
    grounded: true, ready: true, conflicts: [], judge_reason: "", ungraded_reason: "",
  },
  {
    shard: lowConf, suggestion: "review", confidence: 0.15,
    reasons: ["weak signal"], duplicate_of: null, judged: true,
    grounded: null, ready: null, conflicts: [], judge_reason: "", ungraded_reason: "",
  },
];

const publishedShard: Shard = {
  id: "p1", text: "Fail fast always — no retries on HTTP", scope: "global",
  source: "", status: "published", origin: "human", item_id: null,
  project_id: "core", fresh: false, scoring_source: "", auto_confidence: null,
  created_at: "2026-08-01T10:00:00Z",
};

const { publishSpy, rejectSpy, undoSpy, promoteSpy } = vi.hoisted(() => ({
  publishSpy: vi.fn(async () => ({})),
  rejectSpy: vi.fn(async () => ({})),
  undoSpy: vi.fn(async () => ({})),
  promoteSpy: vi.fn(async () => ({ published: "", rejected: [] })),
}));

const project = {
  id: "core", name: "Core", accent: "#a78bfa", visibility: "private", description: "",
  share_global_memory: false, auto_extract: true, mcp_enabled: true, embed_model: "",
  memory_auto_reject: true, memory_write_mode: "review", memory_llm_judge: false,
  agent_adjudication: false, allow_self_review: false,
};

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [project]),
    counts: vi.fn(async () => ({ items: 0, items_in_progress: 0, requests: 0, review: 3 })),
    candidateShards: vi.fn(async () => [candidate, conflictCandidate, dupeCandidate, lowConf]),
    candidateClusters: vi.fn(async () => []),
    scoredCandidates: vi.fn(async () => scored),
    autoActions: vi.fn(async () => [staleAuto, unvettedAuto]),
    shards: vi.fn(async () => [publishedShard]),
    publishShard: publishSpy,
    rejectShard: rejectSpy,
    promoteCluster: promoteSpy,
    undoAutoShard: undoSpy,
    judgeStatus: vi.fn(async () => ({ judge_on: false, judge: "", decider_configured: false, credential_label: "", falling_back: false, reason: "" })),
    judgeShard: vi.fn(async () => ({ shard_id: "", verdict: null, cause: "no_provider", cause_detail: "" })),
  },
}));

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/memory"]}>
        <ProjectProvider>
          <MemoryReviewView />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Memory (merged page) — sweep preview", () => {
  it("opens a preview modal before rejecting low-confidence shards", async () => {
    const user = userEvent.setup();
    renderView();
    const sweepBtn = await screen.findByText(/Sweep.*low-confidence/);
    await user.click(sweepBtn);
    const modal = await screen.findByText(/This will reject/);
    const modalContainer = modal.closest(".fixed") as HTMLElement;
    expect(within(modalContainer).getByText(/Weak signal: maybe use a cache layer/)).toBeInTheDocument();
    expect(within(modalContainer).getByRole("button", { name: /Reject 1/ })).toBeInTheDocument();

    await user.click(within(modalContainer).getByRole("button", { name: /Reject 1/ }));
    expect(rejectSpy).toHaveBeenCalledWith("c4");
  });

  it("Escape closes the preview without rejecting anything", async () => {
    rejectSpy.mockClear();
    const user = userEvent.setup();
    renderView();
    await user.click(await screen.findByText(/Sweep.*low-confidence/));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(rejectSpy).not.toHaveBeenCalled();
  });

  it("undo toast calls undoAutoShard for each swept id", async () => {
    const user = userEvent.setup();
    renderView();
    await user.click(await screen.findByText(/Sweep.*low-confidence/));
    await user.click(await screen.findByRole("button", { name: /Reject 1/ }));
    expect(rejectSpy).toHaveBeenCalledWith("c4");

    // The auto-actions lane has its own Undo buttons; the toast's is the one beside the message.
    const toast = (await screen.findByText(/Swept 1 low-confidence/)).parentElement as HTMLElement;
    await user.click(within(toast).getByRole("button", { name: /Undo/ }));
    expect(undoSpy).toHaveBeenCalledWith("c4");
  });
});

describe("Memory (merged page) — stale flag", () => {
  it("flags unreviewed shards older than 7 days, and not fresh ones", async () => {
    renderView();
    const old = await screen.findByText(/Use retry with exponential backoff/);
    const oldCard = old.closest("div.rounded-\\[12px\\]") as HTMLElement;
    expect(within(oldCard).getByText("stale")).toBeInTheDocument();

    const fresh = { ...candidate, id: "cf", text: "Fresh candidate note", created_at: new Date().toISOString() };
    const { api } = await import("@/lib/api");
    vi.mocked(api.candidateShards).mockResolvedValue([fresh]);
    vi.mocked(api.autoActions).mockResolvedValue([]);
    renderView();
    const freshText = await screen.findByText("Fresh candidate note");
    const freshCard = freshText.closest("div.rounded-\\[12px\\]") as HTMLElement;
    expect(within(freshCard).queryByText("stale")).not.toBeInTheDocument();
    vi.mocked(api.candidateShards).mockResolvedValue([candidate, conflictCandidate, dupeCandidate, lowConf]);
    vi.mocked(api.autoActions).mockResolvedValue([staleAuto, unvettedAuto]);
  });
});

describe("Memory (merged page) — recurring cluster", () => {
  it("calls promoteCluster with the representative and drops the members", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.candidateClusters).mockResolvedValue([
      { size: 2, representative: dupeCandidate, members: [candidate] },
    ]);
    const user = userEvent.setup();
    renderView();
    await user.click(await screen.findByRole("button", { name: /Publish as principle · drop 1/ }));
    expect(promoteSpy).toHaveBeenCalledWith("c3", ["c1"]);
    vi.mocked(api.candidateClusters).mockResolvedValue([]);
  });
});

describe("Memory (merged page) — error state", () => {
  it("renders PlannerError with retry on fetch failure", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.candidateShards).mockRejectedValue(new Error("Memory review queue timed out after 30s"));
    renderView();
    expect(await screen.findByText(/taking too long to load/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    vi.mocked(api.candidateShards).mockResolvedValue([candidate, conflictCandidate, dupeCandidate, lowConf]);
  });
});
