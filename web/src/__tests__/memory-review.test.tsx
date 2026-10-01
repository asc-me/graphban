import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MemoryReviewView } from "@/features/memory/MemoryReviewView";
import { MemoryRouter } from "react-router-dom";

import { ProjectProvider } from "@/features/ProjectContext";
import { settingsPath } from "@/lib/routes";
import type { CandidateJudge, JudgeStatus, Shard } from "@/lib/types";

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

const autoDecider: Shard = {
  id: "m12", text: "Always pin the pgvector image to pg16 in CI.", scope: "global", source: "",
  status: "published", origin: "agent:loop-agent", item_id: null, project_id: "core",
  fresh: true, scoring_source: "decider", auto_confidence: 0.91, created_at: "",
};

// Published with NO human involved (AL-280 trusted / AL-282 agent). Once agents run the
// loop these are the reviewer's actual job, so they get their own label and filter.
const unvetted: Shard = {
  id: "m11", text: "Published by the agent while you were away.", scope: "global", source: "",
  status: "published", origin: "agent:loop-agent", item_id: null, project_id: "core",
  fresh: true, scoring_source: "agent", auto_confidence: 0.93, created_at: "",
};

// Hoisted so the (hoisted) vi.mock factory can reference the spies eagerly.
const { publishSpy, undoSpy, judgeSpy, judgeStatusSpy } = vi.hoisted(() => ({
  publishSpy: vi.fn(async () => ({})),
  undoSpy: vi.fn(async () => ({})),
  judgeSpy: vi.fn(async (id: string): Promise<CandidateJudge> => ({
    shard_id: id,
    verdict: null,
    cause: "no_provider",
    cause_detail: "no independent chat model is configured for this project",
  })),
  // Healthy by default: every test that predates GRPH-995 must keep rendering no banner.
  judgeStatusSpy: vi.fn(async (): Promise<JudgeStatus> => ({
    judge_on: true,
    judge: "decider",
    decider_configured: true,
    credential_label: "Laya",
    falling_back: false,
    reason: "",
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
    counts: vi.fn(async () => ({ items: 0, items_in_progress: 0, requests: 0, review: 1 })),
    candidateShards: vi.fn(async () => [candidate]),
    candidateClusters: vi.fn(async () => []),
    scoredCandidates: vi.fn(async () => []),
    autoActions: vi.fn(async () => [autoRejected, unvetted, autoDecider]),
    publishShard: publishSpy,
    rejectShard: vi.fn(async () => ({ ...candidate, status: "rejected" })),
    promoteCluster: vi.fn(async () => ({ published: "", rejected: [] })),
    undoAutoShard: undoSpy,
    judgeShard: judgeSpy,
    judgeStatus: judgeStatusSpy,
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

// Reset OUTSIDE every describe, so a test that makes the judge mute cannot leak into the next
// one. The banner is the assertion here, and a leaked banner reads as a passing test.
beforeEach(() => {
  judgeStatusSpy.mockResolvedValue({
    judge_on: true,
    judge: "decider",
    decider_configured: true,
    credential_label: "Laya",
    falling_back: false,
    reason: "",
  });
});

describe("Memory review queue", () => {
  beforeEach(async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.candidateShards).mockResolvedValue([candidate]);
  });

  it("shows retry when the queue fetch fails instead of an endless skeleton (GRPH-916)", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.candidateShards).mockRejectedValue(
      new Error("Memory review queue timed out after 30s"),
    );
    renderView();
    expect(await screen.findByText(/taking too long to load/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Loading memory review")).not.toBeInTheDocument();
  });

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
    expect(screen.getByRole("link", { name: "published m1: we never batch writes" })).toBeInTheDocument();
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

  it("renders unscored_budget as not scored yet, not a low score (PRD-45 D9)", async () => {
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
        ungraded_reason: "not scored yet — the decider queue budget ran out this pass",
        judge_source: "",
      },
    ]);
    renderView();
    expect(await screen.findByText("not scored yet")).toBeInTheDocument();
    expect(screen.queryByText(/not judged/)).not.toBeInTheDocument();
    expect(screen.queryByText("ungrounded")).not.toBeInTheDocument();
  });

  it("badges a decider verdict on the review card (PRD-45 D5)", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.scoredCandidates).mockResolvedValueOnce([
      {
        shard: candidate,
        suggestion: "accept",
        confidence: 0.8,
        reasons: ["decider: grounded 0.91 · ready 0.84"],
        duplicate_of: null,
        judged: true,
        grounded: true,
        ready: true,
        conflicts: ["m_abc123"],
        judge_reason: "decider: grounded 0.91 · ready 0.84",
        ungraded_reason: "",
        judge_source: "decider",
      },
    ]);
    renderView();
    expect(await screen.findByRole("link", { name: "m_abc123" })).toHaveAttribute("href", "/memory-triage");
    const card = screen.getByText(/Agent guess: batch writes/).closest("div")!;
    expect(within(card).getByText("decider")).toBeInTheDocument();
    expect(within(card).getByText("grounded")).toBeInTheDocument();
  });
});

describe("unreviewed shards (AL-287)", () => {
  it("labels a shard nobody reviewed differently from a scorer decision", async () => {
    renderView();
    // The scorer's own decision and an unvetted publish must not read the same — the
    // whole point is telling apart "the scorer was confident" from "nobody looked".
    expect(await screen.findByText("agent + judge")).toBeInTheDocument();
    expect(screen.getByText("similarity")).toBeInTheDocument();
    expect(screen.getByText("decider")).toBeInTheDocument();
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

  it("does not show a keep/quality score before anyone asks", async () => {
    renderView();
    const ask = await screen.findByRole("button", { name: /Ask the judge/ });
    expect(ask).toHaveAttribute("title", expect.stringMatching(/keep\/quality/i));
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

describe("a judge that cannot answer (GRPH-995)", () => {
  // The incident: a decider row that probed `valid` — the probe only asks about health — and
  // then failed every `decide()`. Nothing on this page said so.
  const MUTE: JudgeStatus = {
    judge_on: true,
    judge: "similarity",
    decider_configured: true,
    credential_label: "Laya (empty model)",
    falling_back: true,
    reason:
      "the decider (Laya (empty model)) could not be reached: decide() failed at runtime: " +
      "RuntimeError: model '' is not served by this endpoint",
  };

  it("says the project is falling back to similarity, and names what failed", async () => {
    judgeStatusSpy.mockResolvedValue(MUTE);
    renderView();

    const banner = await screen.findByTestId("judge-falling-back");
    expect(banner.textContent).toMatch(/falling back to similarity/i);
    expect(banner.textContent).toContain("Laya (empty model)");
    expect(banner.textContent).toContain("decide() failed at runtime");
    // The remedy is a credential, so the copy has to lead to where credentials are.
    expect(within(banner).getByRole("link", { name: /fix the credential/i })).toHaveAttribute(
      "href",
      settingsPath("deployment/providers"),
    );
  });

  it("says so over an EMPTY queue — an empty queue is not a clean one", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.candidateShards).mockResolvedValueOnce([]);
    vi.mocked(api.autoActions).mockResolvedValueOnce([]);
    judgeStatusSpy.mockResolvedValue(MUTE);
    renderView();

    expect(await screen.findByText(/Nothing to review/)).toBeInTheDocument();
    expect(screen.getByTestId("judge-falling-back")).toBeInTheDocument();
  });

  it("renders nothing when the judge can answer", async () => {
    renderView();

    expect(await screen.findByText(/Agent guess: batch writes/)).toBeInTheDocument();
    expect(screen.queryByTestId("judge-falling-back")).not.toBeInTheDocument();
  });

  it("renders nothing when the judge is OFF — a choice is not a fallback", async () => {
    judgeStatusSpy.mockResolvedValue({
      judge_on: false,
      judge: "off",
      decider_configured: false,
      credential_label: "",
      falling_back: false,
      reason: "llm judge is off for this project",
    });
    renderView();

    expect(await screen.findByText(/Agent guess: batch writes/)).toBeInTheDocument();
    expect(screen.queryByTestId("judge-falling-back")).not.toBeInTheDocument();
  });
});
