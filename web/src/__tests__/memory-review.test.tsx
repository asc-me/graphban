import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MemoryReviewView } from "@/features/memory/MemoryReviewView";
import { MemoryRouter } from "react-router-dom";

import { ProjectProvider } from "@/features/ProjectContext";
import { settingsPath } from "@/lib/routes";
import type { CandidateJudge, JudgeStatus, ScoredCandidate, Shard } from "@/lib/types";

const candidate: Shard = {
  id: "m9", text: "Agent guess: batch writes for perf.", scope: "item", source: "lesson from AL-12",
  status: "candidate", origin: "agent:loop-agent", item_id: "AL-12", project_id: "core",
  fresh: true, scoring_source: "", auto_confidence: null, created_at: "",
};

const plain: ScoredCandidate = {
  shard: candidate, suggestion: "review", confidence: 0.4, reasons: [], duplicate_of: null,
  judged: false, grounded: null, ready: null, conflicts: [], judge_reason: "", ungraded_reason: "",
};

// An auto-rejected near-duplicate: the scorer's call, not a published row nobody looked at.
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

// Published with NO human involved (AL-280 trusted / AL-282 agent).
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

const ok = (count: number) => ({ count, state: "ok", error: "" });

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [project]),
    counts: vi.fn(async () => ({ items: 0, items_in_progress: 0, requests: 0, review: 1 })),
    memoryQueues: vi.fn(async () => ({
      needs_decision: ok(1), conflicts: ok(0), duplicate_clusters: ok(0), stale_or_wrong: ok(0), published_unreviewed: ok(2),
    })),
    scoredCandidates: vi.fn(),
    candidateClusters: vi.fn(async () => []),
    staleShards: vi.fn(async () => []),
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
      <MemoryRouter initialEntries={["/memory"]}>
        <ProjectProvider>
        <MemoryReviewView />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const panel = () => screen.getByTestId("memory-detail");

// Reset OUTSIDE every describe, so a test that makes the judge mute cannot leak into the next
// one. The banner is the assertion here, and a leaked banner reads as a passing test.
beforeEach(async () => {
  judgeStatusSpy.mockResolvedValue({
    judge_on: true,
    judge: "decider",
    decider_configured: true,
    credential_label: "Laya",
    falling_back: false,
    reason: "",
  });
  const { api } = await import("@/lib/api");
  vi.mocked(api.scoredCandidates).mockResolvedValue([plain]);
});

describe("Memory review queue", () => {
  it("shows retry when the queue fetch fails instead of an endless skeleton (GRPH-916)", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.scoredCandidates).mockRejectedValue(new Error("timed out"));
    renderView();
    expect(await screen.findByText("Needs a decision unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Loading memory review")).not.toBeInTheDocument();
  });

  it("shows candidates and publishes one from the detail panel", async () => {
    const user = userEvent.setup();
    renderView();

    expect((await screen.findAllByText(/Agent guess: batch writes/)).length).toBeGreaterThan(0);
    expect(within(panel()).getByText("agent:loop-agent")).toBeInTheDocument();
    // The judge is off for this project: asking it is disabled, and says why.
    expect(within(panel()).getByRole("button", { name: /Ask the judge/ })).toBeDisabled();

    await user.click(within(panel()).getByRole("button", { name: /^Publish/ }));
    expect(publishSpy).toHaveBeenCalledWith("m9");
  });

  it("Undo after a publish returns the shard to the queue", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findAllByText(/Agent guess: batch writes/);
    await user.click(within(panel()).getByRole("button", { name: /^Publish/ }));
    await user.click(await screen.findByRole("button", { name: "Undo" }));
    expect(undoSpy).toHaveBeenCalledWith("m9");
  });
});

describe("review judge signals (GRPH-79)", () => {
  it("shows ungrounded and not-ready without looking like a publish", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.scoredCandidates).mockResolvedValue([
      { ...plain, judged: true, grounded: false, ready: false, conflicts: ["published m1: we never batch writes"] },
    ]);
    renderView();
    const list = await screen.findByRole("listbox", { name: "Needs a decision" });
    expect(within(list).getByText("ungrounded")).toBeInTheDocument();
    expect(within(list).getByText("not ready")).toBeInTheDocument();
    expect(within(panel()).getByText("ungrounded · not ready")).toBeInTheDocument();
    expect(screen.queryByText(/not judged/)).not.toBeInTheDocument();
  });

  it("lists what a conflicting candidate contradicts in the Conflicts queue", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.scoredCandidates).mockResolvedValue([
      { ...plain, judged: true, grounded: false, ready: true, conflicts: ["published m1: we never batch writes"] },
    ]);
    const user = userEvent.setup();
    renderView();
    await screen.findAllByText(/Agent guess: batch writes/);
    await user.click(screen.getByTestId("queue-conflicts"));
    expect(within(panel()).getByText(/Contradicts published/i)).toBeInTheDocument();
    expect(within(panel()).getByText("published m1: we never batch writes")).toBeInTheDocument();
  });

  it("names an ungraded judge rather than showing a quiet zero", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.scoredCandidates).mockResolvedValue([
      { ...plain, confidence: 0.3, ungraded_reason: "stub cannot judge substance" },
    ]);
    renderView();
    expect(await screen.findByText(/not judged — stub cannot judge substance/)).toBeInTheDocument();
    expect(screen.queryByText("ungrounded")).not.toBeInTheDocument();
  });

  it("renders unscored_budget as not scored yet, not a low score (PRD-45 D9)", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.scoredCandidates).mockResolvedValue([
      { ...plain, ungraded_reason: "not scored yet — the decider queue budget ran out this pass" },
    ]);
    renderView();
    expect((await screen.findAllByText("not scored yet")).length).toBeGreaterThan(0);
    expect(screen.queryByText(/not judged/)).not.toBeInTheDocument();
  });

  it("badges a decider verdict (PRD-45 D5)", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.scoredCandidates).mockResolvedValue([
      { ...plain, judged: true, grounded: true, ready: true, judge_source: "decider", confidence: 0.8 },
    ]);
    renderView();
    const list = await screen.findByRole("listbox", { name: "Needs a decision" });
    expect(within(list).getByText("decider")).toBeInTheDocument();
    expect(within(panel()).getByText("decider · grounded · ready")).toBeInTheDocument();
  });
});

describe("unreviewed shards (AL-287)", () => {
  it("labels a shard nobody reviewed differently from a scorer decision", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findAllByText(/Agent guess: batch writes/);
    await user.click(screen.getByTestId("queue-unvetted"));
    const list = await screen.findByRole("listbox", { name: "Published, unreviewed" });
    // "the scorer was confident" and "nobody looked" must not read the same.
    expect(within(list).getByText("agent + judge")).toBeInTheDocument();
    expect(within(list).getByText("decider")).toBeInTheDocument();
    // An auto-REJECT is not published, so it is not in this queue.
    expect(within(list).queryByText(/Duplicate: batch writes/)).not.toBeInTheDocument();
  });

  it("retracts an unreviewed publish back to the queue", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findAllByText(/Agent guess: batch writes/);
    await user.click(screen.getByTestId("queue-unvetted"));
    await screen.findByRole("listbox", { name: "Published, unreviewed" });
    await user.keyboard("r");
    expect(undoSpy).toHaveBeenCalledWith("m11");
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
    await screen.findAllByText(/Agent guess: batch writes/);
    expect(within(panel()).getByRole("button", { name: /Ask the judge/ })).toBeEnabled();
    expect(screen.queryByText(/^Judge:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Judge unavailable/)).not.toBeInTheDocument();
  });

  it("shows unavailable copy, not a quality number, when the judge cannot run", async () => {
    renderView();
    await screen.findAllByText(/Agent guess: batch writes/);
    await userEvent.click(within(panel()).getByRole("button", { name: /Ask the judge/ }));
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
    await screen.findAllByText(/Agent guess: batch writes/);
    await userEvent.click(within(panel()).getByRole("button", { name: /Ask the judge/ }));
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
    vi.mocked(api.scoredCandidates).mockResolvedValue([]);
    judgeStatusSpy.mockResolvedValue(MUTE);
    renderView();

    expect(await screen.findByText(/Queue clear/)).toBeInTheDocument();
    expect(screen.getByTestId("judge-falling-back")).toBeInTheDocument();
  });

  it("renders nothing when the judge can answer", async () => {
    renderView();

    await screen.findAllByText(/Agent guess: batch writes/);
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

    await screen.findAllByText(/Agent guess: batch writes/);
    expect(screen.queryByTestId("judge-falling-back")).not.toBeInTheDocument();
  });
});
