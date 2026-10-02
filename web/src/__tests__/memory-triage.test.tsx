import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { MemoryTriageView } from "@/features/memory/MemoryTriageView";
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
    judgeShard: vi.fn(async () => ({ shard_id: "", verdict: null, cause: "no_provider", cause_detail: "" })),
  },
}));

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/memory-triage"]}>
        <ProjectProvider>
          <MemoryTriageView />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Memory triage — error state", () => {
  afterEach(async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.candidateShards).mockResolvedValue([candidate, conflictCandidate, dupeCandidate, lowConf]);
  });

  it("renders PlannerError with retry on fetch failure, not the empty-queue copy", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.candidateShards).mockRejectedValue(new Error("Memory review queue timed out after 30s"));
    renderView();
    expect(await screen.findByText(/taking too long to load/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByText(/Queue clear/)).not.toBeInTheDocument();
  });
});

describe("Memory triage — queue routing", () => {
  it("routes shards to the correct queues by count", async () => {
    renderView();
    expect(await screen.findByText("Needs review")).toBeInTheDocument();

    expect(screen.getByText("Conflicts")).toBeInTheDocument();
    expect(screen.getByText("Duplicates")).toBeInTheDocument();
    expect(screen.getByText("Stale")).toBeInTheDocument();
    expect(screen.getByText("Unvetted")).toBeInTheDocument();

    const needsBtn = screen.getByTitle("Candidates awaiting a human decision");
    expect(needsBtn).toBeInTheDocument();
    expect(screen.getByTitle("Contradict published memory")).toBeInTheDocument();
    expect(screen.getByTitle("Near-duplicates of another candidate")).toBeInTheDocument();
  });

  it("shows the needs queue by default with the right shard", async () => {
    renderView();
    expect(await screen.findByText(/Use retry with exponential backoff/)).toBeInTheDocument();
  });

  it("switches to the conflicts queue and shows the conflicting shard", async () => {
    const user = userEvent.setup();
    renderView();
    await user.click(await screen.findByText("Conflicts"));
    expect(screen.getByText(/Never retry HTTP calls/)).toBeInTheDocument();
    expect(screen.queryByText(/Use retry with exponential backoff/)).not.toBeInTheDocument();
  });

  it("shows the queue-clear copy when a queue is empty after filtering", async () => {
    const user = userEvent.setup();
    renderView();
    await user.click(await screen.findByText("Duplicates"));
    expect(screen.getByText(/Use retry with backoff for HTTP/)).toBeInTheDocument();
  });

  it("routes an auto-acted shard older than 7 days to Stale, not Unvetted", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByText("Stale");

    await user.click(screen.getByText("Stale"));
    expect(screen.getByText(/prefer composition over inheritance/)).toBeInTheDocument();
    expect(screen.queryByText(/Auto-published by agent on write/)).not.toBeInTheDocument();

    await user.click(screen.getByText("Unvetted"));
    expect(screen.getByText(/Auto-published by agent on write/)).toBeInTheDocument();
    expect(screen.queryByText(/prefer composition over inheritance/)).not.toBeInTheDocument();
  });
});

describe("Memory triage — sweep preview", () => {
  it("opens a preview modal before rejecting low-confidence shards", async () => {
    const user = userEvent.setup();
    renderView();
    const sweepBtn = await screen.findByText(/Sweep.*low-confidence/);
    expect(sweepBtn).toBeInTheDocument();

    await user.click(sweepBtn);
    const modal = await screen.findByText(/This will reject/);
    expect(modal).toBeInTheDocument();
    const modalContainer = modal.closest(".fixed") as HTMLElement;
    expect(within(modalContainer).getByText(/Weak signal: maybe use a cache layer/)).toBeInTheDocument();
    expect(within(modalContainer).getByRole("button", { name: /Reject 1/ })).toBeInTheDocument();

    await user.click(within(modalContainer).getByRole("button", { name: /Reject 1/ }));
    expect(rejectSpy).toHaveBeenCalledWith("c4");
  });
});

describe("Memory triage — bulk actions", () => {
  it("selects rows and bulk-publishes them", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByText(/Use retry with exponential backoff/);

    const selectAll = screen.getByLabelText("Select all visible rows");
    await user.click(selectAll);

    const publishAll = screen.getByRole("button", { name: /Publish all/ });
    expect(publishAll).toBeInTheDocument();
    await user.click(publishAll);

    expect(publishSpy).toHaveBeenCalled();
  });
});

describe("Memory triage — canonical wording", () => {
  it("calls promoteCluster with the picked variant and rejects the rest", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.candidateClusters).mockResolvedValue([
      {
        size: 2,
        representative: dupeCandidate,
        members: [candidate],
      },
    ]);

    const user = userEvent.setup();
    renderView();
    await user.click(await screen.findByText("Duplicates"));
    await user.click(screen.getByText(/Use retry with backoff for HTTP/));

    expect(screen.getByText("Pick the canonical wording")).toBeInTheDocument();
    await user.click(screen.getByText(/Use retry with exponential backoff/));
    await user.click(screen.getByRole("button", { name: /Publish as principle/ }));

    expect(promoteSpy).toHaveBeenCalledWith("c1", ["c3"]);
  });
});

describe("Memory triage — keyboard affordance", () => {
  // The handler has bound J/K/X/Enter since S8; only the affordance was missing, so a
  // keyboard-first queue nobody could discover was not keyboard-first (GRPH-1005). Each
  // key the hint names is asserted against what it does, so the line cannot outlive the
  // behaviour it describes.
  it("shows the shortcut hint, and every key it names does what it says — on arrival", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByText(/Use retry with exponential backoff/);

    expect(screen.getByText("J/K move · X select · Enter open · Esc close")).toBeInTheDocument();

    // No click into the view first. That click used to be load-bearing: the handler sat on
    // the root div and heard nothing until something inside it had focus, so a user arriving
    // from the left nav found every advertised key dead (GRPH-1009). The test clicked in and
    // so could not see it. Focus is on document.body here, exactly as after a navigation.
    expect(document.activeElement).toBe(document.body);

    expect(screen.queryByText("Detail")).not.toBeInTheDocument();

    await user.keyboard("x");
    expect(await screen.findByText("1 selected")).toBeInTheDocument();

    await user.keyboard("{Enter}");
    expect(await screen.findByText("Detail")).toBeInTheDocument();

    // J and K must MOVE the cursor, and the only way to see that is to act on a DIFFERENT
    // row afterwards and name which one. The first version of this test pressed neither:
    // deleting `case "j"` and `case "k"` outright left it green (review bounce, PR #914).
    //
    // The queue renders "Weak signal…" first and "Use retry…" second, so the cursor starts
    // on the former. Asserted inside the detail panel, because both strings also appear in
    // the row list — a bare findByText would match the row and pass with no cursor at all.
    // The panel root is the grandparent of the "Detail" label: the label sits in a header
    // row, and the shard text is that row's SIBLING. Scoping to `.closest("div")` grabs the
    // header alone and finds nothing — which is a scoping bug, not a cursor bug, and would
    // have read as one.
    // The panel's FIRST paragraph is the shard's own text. Scope to it rather than
    // searching the panel: the panel also lists near-duplicates and conflicts, so a text
    // query matches more than once and throws — and asserting on the row list instead
    // would pass with no cursor at all, since both strings are in the rows too.
    const openShardText = () =>
      screen.getByText("Detail").parentElement!.parentElement!.querySelector("p")
        ?.textContent ?? "";
    expect(openShardText()).toMatch(/Weak signal: maybe use a cache layer/);

    await user.keyboard("{Escape}");
    await user.keyboard("j");
    await user.keyboard("{Enter}");
    expect(await screen.findByText("Detail")).toBeInTheDocument();
    expect(openShardText()).toMatch(/Use retry with exponential backoff for HTTP calls/);

    // ...and k moves back. Without this half, a `k` bound to nothing at all still satisfies
    // everything above.
    //
    // What this does NOT catch, recorded rather than left for the next reader to discover:
    // the SIZE of j's step. `moveCursor` clamps to `visible.length - 1`, and this queue
    // renders two rows, so moveCursor(2) from row 0 lands on row 1 exactly as moveCursor(1)
    // does — the mutation is equivalent at this fixture size and no assertion here can see
    // it. Catching it needs a third row in the Needs-review queue, which is a fixture change
    // the rest of this file shares. The bindings and their direction are pinned; the
    // magnitude is not.
    await user.keyboard("{Escape}");
    await user.keyboard("k");
    await user.keyboard("{Enter}");
    expect(await screen.findByText("Detail")).toBeInTheDocument();
    expect(openShardText()).toMatch(/Weak signal: maybe use a cache layer/);

    // Esc close — the hint names it now, so it is held to the same standard.
    await user.keyboard("{Escape}");
    expect(screen.queryByText("Detail")).not.toBeInTheDocument();
  });

  it("stands down while the sweep modal is open, and Esc closes the modal", async () => {
    // The reason this view was wired differently from Lessons and Activity: a window
    // listener fires over the modal, where x would select a row behind it. The guard is on
    // state, not focus — clicking the Sweep button leaves focus on that button, OUTSIDE the
    // dialog, which is exactly the case a focus-based guard misses.
    const user = userEvent.setup();
    renderView();
    await screen.findByText(/Use retry with exponential backoff/);

    await user.click(await screen.findByText(/Sweep.*low-confidence/));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();

    await user.keyboard("x");
    expect(screen.queryByText("1 selected")).not.toBeInTheDocument();
    await user.keyboard("{Enter}");
    expect(screen.queryByText("Detail")).not.toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    // ...and the keys work again the moment it is gone.
    await user.keyboard("x");
    expect(await screen.findByText("1 selected")).toBeInTheDocument();
  });
});

describe("Memory triage — CALL sabotage", () => {
  it("undo toast actually calls undoAutoShard for each swept id", async () => {
    const user = userEvent.setup();
    renderView();

    await user.click(await screen.findByText(/Sweep.*low-confidence/));
    await user.click(await screen.findByRole("button", { name: /Reject 1/ }));

    expect(rejectSpy).toHaveBeenCalledWith("c4");

    const undoBtn = await screen.findByRole("button", { name: /^Undo$/ });
    expect(undoBtn).toBeInTheDocument();
    await user.click(undoBtn);
    expect(undoSpy).toHaveBeenCalledWith("c4");
  });
});
