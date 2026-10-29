import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MemoryReviewView } from "@/features/memory/MemoryReviewView";
import { ProjectProvider } from "@/features/ProjectContext";
import type { MemoryQueueCount, MemoryQueueKey, ScoredCandidate, Shard, StaleShardRow } from "@/lib/types";

// GRPH-1176: the Memory triage console — queue cards, keyboard, bulk, search, failure states.

const RECENT = new Date(Date.now() - 2 * 3_600_000).toISOString();
const OLD = new Date(Date.now() - 20 * 86_400_000).toISOString();

const shard = (id: string, text: string, extra: Partial<Shard> = {}): Shard => ({
  id, text, scope: "item", source: "", status: "candidate", origin: "agent:worker-1",
  item_id: null, project_id: "core", fresh: true, scoring_source: "", auto_confidence: null,
  shard_class: "",
  created_at: RECENT, ...extra,
});

const scoredRow = (s: Shard, confidence: number, extra: Partial<ScoredCandidate> = {}): ScoredCandidate => ({
  shard: s, suggestion: "review", confidence, reasons: [], duplicate_of: null, judged: true,
  grounded: true, ready: true, conflicts: [], judge_reason: "", ungraded_reason: "", ...extra,
});

const s1 = shard("s1", "Prefer claim_cluster over claim_next for workers.");
const s2 = shard("s2", "Roadmap renders a named empty board when nothing is scheduled.", { origin: "agent:planner" });
const s3 = shard("s3", "Surface flags can be toggled without a restart.", { created_at: OLD });
const SCORED: ScoredCandidate[] = [
  scoredRow(s1, 0.92),
  scoredRow(s2, 0.6, { ready: false }),
  scoredRow(s3, 0.5, { grounded: false, conflicts: ["published p1: flags are read once at boot"] }),
];

const d1 = shard("d1", "Run migrations with --plan before applying on staging.");
const d2 = shard("d2", "Always --plan migrations on staging.");
const CLUSTERS = [{ size: 2, representative: d1, members: [d2] }];

const st1 = shard("st1", "Wave-5 keys expire at midnight UTC.", { status: "published", created_at: OLD, shard_class: "pitfall" });
const st2 = shard("st2", "Use the ci-runner-3 cache for node builds.", { status: "published", created_at: OLD });
const STALE: StaleShardRow[] = [
  { shard: st1, why: ["nothing has corroborated it in 30+ days"], ungraded: false, ungraded_reason: "",
    recommendation: { action: "retire", confidence: 0.93, reason: "the wave it describes ended" } },
  { shard: st2, why: ["the code it came from no longer exists"], ungraded: true,
    ungraded_reason: "llm judge is off for this project", recommendation: null },
];

const u1 = shard("u1", "End wave revokes only wave-tagged keys.", { status: "published", scoring_source: "agent", auto_confidence: 0.91 });
const u2 = shard("u2", "Duplicate of an existing note.", { status: "rejected", scoring_source: "similarity", auto_confidence: 0.97 });

const ok = (count: number): MemoryQueueCount => ({ count, state: "ok", error: "" });
const COUNTS: Record<MemoryQueueKey, MemoryQueueCount> = {
  needs_decision: ok(3), conflicts: ok(1), duplicate_clusters: ok(1), stale_or_wrong: ok(2), published_unreviewed: ok(1),
};

const spies = vi.hoisted(() => ({
  publish: vi.fn(async (id: string) => ({ id })),
  reject: vi.fn(async (id: string) => ({ id })),
  judge: vi.fn(async (id: string) => ({ shard_id: id, verdict: { keep: true, quality: 0.8, reason: "durable" }, cause: null, cause_detail: "" })),
  promote: vi.fn(async () => ({ published: "", rejected: [] })),
  undoAuto: vi.fn(async (id: string) => ({ id })),
  retire: vi.fn(async (id: string) => ({ id })),
  restore: vi.fn(async (id: string) => ({ id })),
  keep: vi.fn(async (id: string) => ({ id })),
  setClass: vi.fn(async (id: string) => ({ id })),
  apply: vi.fn(async (ids: string[]) => ids.map((id) => ({ shard_id: id, action: "retire", confidence: 0.93, applied: true, reason: "", undo: "restore" }))),
}));

const project = {
  id: "core", name: "Core", tag: "CORE", accent: "#a78bfa", visibility: "private", description: "",
  share_global_memory: false, auto_extract: true, mcp_enabled: true, embed_model: "",
  memory_auto_reject: true, memory_write_mode: "review", memory_llm_judge: true,
  agent_adjudication: false, allow_self_review: false,
};

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [project]),
    counts: vi.fn(async () => ({ items: 0, items_in_progress: 0, requests: 0, review: 3 })),
    memoryQueues: vi.fn(),
    scoredCandidates: vi.fn(),
    candidateClusters: vi.fn(),
    staleShards: vi.fn(),
    autoActions: vi.fn(),
    judgeStatus: vi.fn(async () => ({ judge_on: true, judge: "decider", decider_configured: true, credential_label: "", falling_back: false, reason: "" })),
    publishShard: spies.publish,
    rejectShard: spies.reject,
    judgeShard: spies.judge,
    promoteCluster: spies.promote,
    undoAutoShard: spies.undoAuto,
    retireShard: spies.retire,
    restoreShard: spies.restore,
    keepShard: spies.keep,
    setShardClass: spies.setClass,
    applyRecommendations: spies.apply,
  },
}));

beforeEach(async () => {
  const { api } = await import("@/lib/api");
  vi.mocked(api.memoryQueues).mockResolvedValue(COUNTS);
  vi.mocked(api.scoredCandidates).mockResolvedValue(SCORED);
  vi.mocked(api.candidateClusters).mockResolvedValue(CLUSTERS);
  vi.mocked(api.staleShards).mockResolvedValue(STALE);
  vi.mocked(api.autoActions).mockResolvedValue([u1, u2]);
  Object.values(spies).forEach((s) => s.mockClear());
});

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

const activeId = () => screen.getAllByRole("option").find((o) => o.getAttribute("aria-selected") === "true")?.getAttribute("data-row-id");
const ready = () => screen.findAllByText(/Prefer claim_cluster/);

describe("queue cards", () => {
  it("renders each of the five counts, and selecting a card switches the list to it", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    const cases: [string, string, string, RegExp][] = [
      ["needs", "Needs a decision", "3", /Prefer claim_cluster/],
      ["conflicts", "Conflicts", "1", /Surface flags can be toggled/],
      ["dupes", "Duplicate clusters", "1", /Run migrations with --plan/],
      ["stale", "Stale or wrong", "2", /Wave-5 keys expire/],
      ["unvetted", "Published, unreviewed", "1", /End wave revokes/],
    ];
    for (const [id, label, count, row] of cases) {
      const card = screen.getByTestId(`queue-${id}`);
      expect(within(card).getByText(label)).toBeInTheDocument();
      expect(within(card).getByText(count)).toBeInTheDocument();
      await user.click(card);
      expect(card).toHaveAttribute("aria-selected", "true");
      const list = await screen.findByRole("listbox", { name: label });
      expect(within(list).getByText(row)).toBeInTheDocument();
    }
    // The unvetted queue is published auto-actions only — an auto-REJECT is not in it.
    expect(screen.queryByText(/Duplicate of an existing note/)).not.toBeInTheDocument();
  });
});

describe("keyboard", () => {
  it("J/K move the selection and X toggles the selected row's checkbox", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    expect(activeId()).toBe("s1");
    await user.keyboard("j");
    expect(activeId()).toBe("s2");
    await user.keyboard("j");
    expect(activeId()).toBe("s3");
    await user.keyboard("j"); // clamps at the end
    expect(activeId()).toBe("s3");
    await user.keyboard("k");
    expect(activeId()).toBe("s2");

    const box = screen.getByRole("button", { name: "Select s2" });
    expect(box).toHaveAttribute("aria-pressed", "false");
    await user.keyboard("x");
    expect(box).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    await user.keyboard("x");
    expect(box).toHaveAttribute("aria-pressed", "false");
  });

  it("P publishes the selected row", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    await user.keyboard("j");
    await user.keyboard("p");
    await waitFor(() => expect(spies.publish).toHaveBeenCalledWith("s2"));
    expect(spies.publish).toHaveBeenCalledTimes(1);
    expect(spies.reject).not.toHaveBeenCalled();
  });

  it("R rejects the selected row", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    await user.keyboard("r");
    await waitFor(() => expect(spies.reject).toHaveBeenCalledWith("s1"));
    expect(spies.publish).not.toHaveBeenCalled();
  });

  it("A asks the judge about the selected row and shows its answer", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    await user.keyboard("jj");
    await user.keyboard("a");
    await waitFor(() => expect(spies.judge).toHaveBeenCalledWith("s3"));
    expect(await screen.findByText(/Judge: 80% — durable/)).toBeInTheDocument();
    expect(spies.publish).not.toHaveBeenCalled();
  });

  it("keys do nothing while typing in the search box", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    await user.type(screen.getByRole("textbox", { name: "Search memory" }), "prj");
    expect(spies.publish).not.toHaveBeenCalled();
    expect(spies.reject).not.toHaveBeenCalled();
  });
});

describe("bulk", () => {
  it("bulk publish acts on exactly the checked rows", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    await user.click(screen.getByRole("button", { name: "Select s1" }));
    await user.click(screen.getByRole("button", { name: "Select s3" }));
    const bar = screen.getByRole("toolbar", { name: "Bulk actions" });
    expect(within(bar).getByText("2 selected")).toBeInTheDocument();
    await user.click(within(bar).getByRole("button", { name: "Publish" }));
    await waitFor(() => expect(spies.publish).toHaveBeenCalledTimes(2));
    expect(spies.publish.mock.calls.map((c) => c[0]).sort()).toEqual(["s1", "s3"]);
    expect(await screen.findByText("Published 2 notes")).toBeInTheDocument();
  });

  it("in Everywhere, a selection across queues offers no bulk action — a stale row is never rejected", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    await user.click(screen.getByRole("button", { name: "Everywhere" }));
    await user.type(screen.getByRole("textbox", { name: "Search memory" }), "e");
    await screen.findByRole("listbox", { name: "Search results" });
    await user.click(screen.getByRole("button", { name: "Select s1" }));
    await user.click(screen.getByRole("button", { name: "Select st1" }));
    const bar = screen.getByRole("toolbar", { name: "Bulk actions" });
    expect(within(bar).getByText("2 selected")).toBeInTheDocument();
    expect(within(bar).getByTestId("bulk-mixed")).toHaveTextContent(/Needs a decision, Stale or wrong/);
    expect(within(bar).queryByRole("button", { name: "Reject" })).toBeNull();
    expect(within(bar).queryByRole("button", { name: "Publish" })).toBeNull();
    expect(spies.reject).not.toHaveBeenCalled();
    // Unchecking the stale row brings the needs-row actions back, scoped to that row alone.
    await user.click(screen.getByRole("button", { name: "Select st1" }));
    await user.click(within(bar).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(spies.reject).toHaveBeenCalledTimes(1));
    expect(spies.reject).toHaveBeenCalledWith("s1");
    expect(spies.reject).not.toHaveBeenCalledWith("st1");
  });

  it("switching search scope clears the selection", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    await user.click(screen.getByRole("button", { name: "Select s1" }));
    expect(screen.getByRole("toolbar", { name: "Bulk actions" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Everywhere" }));
    expect(screen.queryByRole("toolbar", { name: "Bulk actions" })).toBeNull();
  });

  it("the cleanup banner publishes only grounded, ready rows at ≥ 0.85", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    const sweep = screen.getByTestId("memory-sweep");
    await user.click(within(sweep).getByRole("button", { name: "Publish 1" }));
    await waitFor(() => expect(spies.publish).toHaveBeenCalledWith("s1"));
    expect(spies.publish).toHaveBeenCalledTimes(1);
  });

  it("stale Apply sends the checked ids, and Undo restores what was retired", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    await user.click(screen.getByTestId("queue-stale"));
    const sweep = await screen.findByTestId("memory-sweep");
    expect(sweep.textContent).toMatch(/1 retire/);
    await user.click(within(sweep).getByRole("button", { name: "Apply 1" }));
    await waitFor(() => expect(spies.apply).toHaveBeenCalledWith(["st1"]));
    await user.click(await screen.findByRole("button", { name: "Undo" }));
    await waitFor(() => expect(spies.restore).toHaveBeenCalledWith("st1"));
  });

  it("an ungraded stale row has nothing to apply, and Keep is available", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    await user.click(screen.getByTestId("queue-stale"));
    await user.click(await screen.findByText(/ci-runner-3 cache/));
    const panel = screen.getByTestId("memory-detail");
    expect(within(panel).getByText(/No recommendation, so nothing to apply/)).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: /Apply recommendation/ })).toBeDisabled();
    expect(within(panel).getByRole("button", { name: /Keep/ })).toBeEnabled();
    await user.click(within(panel).getByRole("button", { name: /^Retire/ }));
    await waitFor(() => expect(spies.retire).toHaveBeenCalledWith("st2"));
  });

  it("Keep on a stale row calls the keep endpoint and the row leaves the stale list", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    await user.click(screen.getByTestId("queue-stale"));
    await user.click(await screen.findByText(/ci-runner-3 cache/));
    const panel = screen.getByTestId("memory-detail");
    await user.click(within(panel).getByRole("button", { name: /^Keep/ }));
    await waitFor(() => expect(spies.keep).toHaveBeenCalledWith("st2"));
    await waitFor(() => expect(screen.queryByText(/ci-runner-3 cache/)).not.toBeInTheDocument());
  });

  it("the CLASS column shows the stored class and a dash when unclassified, never scope", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    await user.click(screen.getByTestId("queue-stale"));
    const rows = await screen.findAllByRole("option");
    const cls = (id: string) => rows.find((r) => r.getAttribute("data-row-id") === id)!;
    expect(within(cls("st1")).getByTestId("row-class")).toHaveTextContent("pitfall");
    expect(within(cls("st2")).getByTestId("row-class")).toHaveTextContent("—");
  });

  it("a class chip PATCHes that class, and the active chip clears it", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    await user.click(screen.getByTestId("queue-stale"));
    await user.click(await screen.findByText(/ci-runner-3 cache/));
    let panel = screen.getByTestId("memory-detail");
    await user.click(within(panel).getByRole("button", { name: "convention" }));
    await waitFor(() => expect(spies.setClass).toHaveBeenCalledWith("st2", "convention"));
    await user.click(await screen.findByText(/Wave-5 keys expire/));
    panel = screen.getByTestId("memory-detail");
    const active = within(panel).getByRole("button", { name: "pitfall" });
    expect(active).toHaveAttribute("aria-pressed", "true");
    await user.click(active);
    await waitFor(() => expect(spies.setClass).toHaveBeenCalledWith("st1", ""));
  });

  it("merges a duplicate cluster into the wording the reviewer picked", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    await user.click(screen.getByTestId("queue-dupes"));
    await screen.findByRole("listbox", { name: "Duplicate clusters" });
    const panel = screen.getByTestId("memory-detail");
    await user.click(within(panel).getByRole("radio", { name: /Always --plan migrations/ }));
    await user.click(within(panel).getByRole("button", { name: /Merge into selected · drop 1/ }));
    await waitFor(() => expect(spies.promote).toHaveBeenCalledWith("d2", ["d1"]));
  });
});

describe("search", () => {
  it("'Everywhere' searches across every queue, not just this one", async () => {
    const user = userEvent.setup();
    renderView();
    await ready();
    await user.type(screen.getByRole("textbox", { name: "Search memory" }), "wave");
    // In this queue (Needs a decision) nothing mentions a wave.
    expect(screen.getByTestId("memory-empty")).toHaveTextContent(/Nothing matches/);

    await user.click(screen.getByRole("button", { name: "Everywhere" }));
    const results = await screen.findByRole("listbox", { name: "Search results" });
    expect(within(results).getByText(/Wave-5 keys expire/)).toBeInTheDocument(); // stale
    expect(within(results).getByText(/End wave revokes/)).toBeInTheDocument(); // unvetted
    expect(within(results).getByText("Stale or wrong")).toBeInTheDocument(); // tagged with its queue
    expect(screen.getByText("RESULTS · ALL QUEUES")).toBeInTheDocument();
    expect(screen.getByText("2 matches")).toBeInTheDocument();
  });
});

describe("failure is not empty", () => {
  it("a failed queue renders as failed — never 'Queue clear' and never 0", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.memoryQueues).mockResolvedValue({
      ...COUNTS, stale_or_wrong: { count: null, state: "failed", error: "judge exploded" },
    });
    vi.mocked(api.staleShards).mockRejectedValue(new Error("500"));
    const user = userEvent.setup();
    renderView();
    await ready();

    const card = screen.getByTestId("queue-stale");
    expect(within(card).getByText("failed")).toBeInTheDocument();
    expect(within(card).queryByText("0")).not.toBeInTheDocument();

    await user.click(card);
    expect(await screen.findByText("Stale or wrong unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByText(/Queue clear/)).not.toBeInTheDocument();
  });

  it("if the counts call itself fails, every card says failed", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.memoryQueues).mockRejectedValue(new Error("502"));
    renderView();
    await ready();
    await waitFor(() =>
      expect(screen.getAllByRole("tab").every((t) => within(t).queryByText("failed"))).toBe(true),
    );
  });

  it("'Everywhere' says which queues did not load instead of passing partial results as whole", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.staleShards).mockRejectedValue(new Error("500"));
    const user = userEvent.setup();
    renderView();
    await ready();
    await user.type(screen.getByRole("textbox", { name: "Search memory" }), "wave");
    await user.click(screen.getByRole("button", { name: "Everywhere" }));
    expect(await screen.findByText(/Did not load: Stale or wrong/)).toBeInTheDocument();
  });
});

describe("empty queue", () => {
  it("says 'Queue clear. Pick another queue above, or search everywhere.' and keeps the cards and policy", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.scoredCandidates).mockResolvedValue([]);
    vi.mocked(api.memoryQueues).mockResolvedValue({ ...COUNTS, needs_decision: ok(0), conflicts: ok(0) });
    renderView();
    expect(
      await screen.findByText("Queue clear. Pick another queue above, or search everywhere."),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("tab")).toHaveLength(5);
    expect(within(screen.getByTestId("queue-needs")).getByText("0")).toBeInTheDocument();
    expect(screen.getByTestId("memory-policy")).toHaveTextContent(/PROJECT POLICY/);
  });
});
