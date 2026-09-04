import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LiveView } from "@/features/live/LiveView";
import { ProjectProvider } from "@/features/ProjectContext";
import type { LiveAgent, LiveBoard, LiveDelegationRow, LiveFeed, LiveUser } from "@/lib/types";

const project = {
  id: "core", tag: "CORE", name: "Core", accent: "#c6f24e", visibility: "private",
  description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
  embed_model: "", credential_id: null, model_override: "",
  memory_auto_reject: true, memory_write_mode: "review", memory_llm_judge: false,
  agent_adjudication: false, allow_self_review: false,
};

function agent(over: Partial<LiveAgent> = {}): LiveAgent {
  return {
    id: "a1",
    key: "CORE-A1",
    label: "clustered-one",
    role: "worker",
    state: "working",
    last_seen_at: "2026-09-02T12:00:00Z",
    worktree: "/tmp/wt",
    branch: "feat/x",
    branch_orphaned: false,
    last_call: null,
    calls_in_window: 0,
    silence_seconds: null,
    call_state: "never",
    status: null,
    status_state: "unreported",
    file_state: "idle",
    files: [],
    holdings: [],
    ...over,
  };
}

function user(over: Partial<LiveUser> = {}): LiveUser {
  return {
    user_id: "u_blair",
    label: "Blair",
    initials: "BL",
    color: "#7ca2ff",
    online: 1,
    total: 1,
    unattributed_calls: 0,
    unattributed_by_key: [],
    agents: [agent()],
    ...over,
  };
}

function emptyBoard(over: Partial<LiveBoard> = {}): LiveBoard {
  return {
    served_at: "2026-09-02T12:00:10Z",
    heartbeat_interval_seconds: 50,
    presence_ttl_seconds: 150,
    truncated: false,
    total_agents: 0,
    unattributed_count: 0,
    by_role: {},
    roles: ["planner", "worker", "reviewer"],
    window_seconds: 500,
    retention_days: 7,
    users: [],
    user_counts: [],
    ...over,
  };
}

let board: LiveBoard = emptyBoard();
let feed: LiveFeed = { served_at: "2026-09-02T12:00:10Z", retention_days: 7, state: "never", rows: [] };

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [project]),
    config: vi.fn(async () => ({ hosted_mode: false, signup_mode: "closed" })),
    live: vi.fn(async () => board),
    liveFeed: vi.fn(async () => feed),
  },
}));

function renderLive(path = "/live") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>
          <Routes>
            <Route path="/live" element={<LiveView />} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Live board", () => {
  beforeEach(() => {
    board = emptyBoard();
  });

  it("names an empty project as unregistered, not idle", async () => {
    renderLive();
    expect(await screen.findByRole("heading", { name: "Live" })).toBeInTheDocument();
    expect(screen.getByText("No agents have registered on this project.")).toBeInTheDocument();
    expect(screen.queryByText(/idle/i)).not.toBeInTheDocument();
  });

  it("renders a row unchanged whether the delegations field is absent, null or present (PRD-35 PR 1)", async () => {
    // PR 1 ships the payload field; PR 2 renders it. A server ahead of this web build must
    // not change what the row shows, and a server behind it must not either.
    const withField = agent({
      id: "a2", key: "CORE-A2", label: "planner-two", delegations: {
        open: 1, claimed: 0, finished: 0, expired: 0, closed: 0, oldest_open_seconds: 240,
        rows: [{
          id: "dlg_1", item: "CORE-9", state: "open", lane: "backend", requested_tier: "cheap",
          declared_tier: null, declared_model: null, mismatch: false, delegated_by: "a2",
          agent_id: null, linked_by: null, outcome: null, closed_reason: null, closed_by: null,
          note: "", created_at: "2026-09-02T11:56:00Z", claimed_at: null, age_seconds: 240,
        }],
      },
    });
    const withNull = agent({ id: "a3", key: "CORE-A3", label: "quiet-three", delegations: null });
    const absent = agent({ id: "a4", key: "CORE-A4", label: "old-server" });
    delete (absent as Partial<LiveAgent>).delegations;
    board = emptyBoard({ total_agents: 3, users: [user({ online: 3, total: 3, agents: [withField, withNull, absent] })] });
    renderLive();
    expect(await screen.findByText("planner-two")).toBeInTheDocument();
    expect(screen.getByText("quiet-three")).toBeInTheDocument();
    expect(screen.getByText("old-server")).toBeInTheDocument();
    // PR 2: null is a word, absent is silence, present is a count line (criterion 15).
    expect(screen.getAllByText("no delegations")).toHaveLength(1);
    expect(screen.getByText(/1: 1 open \(oldest 4m\)/)).toBeInTheDocument();
  });

  it("names an expired delegation as a spawn that never claimed, and a mismatch as such (PRD-35 criteria 15, 16)", async () => {
    const row = (over: Partial<LiveDelegationRow>): LiveDelegationRow => ({
      id: "dlg", item: "CORE-9", state: "open", lane: "backend", requested_tier: "cheap",
      declared_tier: null, declared_model: null, mismatch: false, delegated_by: "a2",
      agent_id: null, linked_by: null, outcome: null, closed_reason: null, closed_by: null,
      note: "", created_at: null, claimed_at: null, age_seconds: 700, ...over,
    });
    const planner = agent({
      id: "a2", key: "CORE-A2", label: "planner-two", delegations: {
        open: 0, claimed: 2, finished: 0, expired: 1, closed: 1, oldest_open_seconds: null,
        rows: [
          row({ id: "d1", item: "CORE-9", state: "expired" }),
          row({ id: "d2", item: "CORE-10", state: "claimed", agent_id: "CORE-A7", linked_by: "parent",
                declared_tier: "frontier", declared_model: "opus-5", mismatch: true }),
          row({ id: "d3", item: "CORE-11", state: "claimed", agent_id: "CORE-A8", linked_by: "seat",
                declared_tier: "undeclared" }),
          row({ id: "d4", item: "CORE-12", state: "closed", closed_reason: "superseded", closed_by: "CORE-A9" }),
        ],
      },
    });
    board = emptyBoard({ total_agents: 1, users: [user({ agents: [planner] })] });
    renderLive();
    expect(await screen.findByText(/4: 2 claimed, 1 expired, 1 closed/)).toBeInTheDocument();
    expect(screen.getByText(/expired, nothing claimed/)).toBeInTheDocument();
    expect(screen.getByText(/claimed by CORE-A7 \(requested cheap, declared frontier, opus-5\)/)).toBeInTheDocument();
    expect(screen.getByText(/claimed by CORE-A8 \(requested cheap, undeclared\)/)).toBeInTheDocument();
    expect(screen.getByText(/superseded by CORE-A9/)).toBeInTheDocument();
    expect(screen.queryByText("no delegations")).not.toBeInTheDocument();
  });

  it("names a filter miss as a person, not an empty project", async () => {
    board = emptyBoard({
      user_counts: [
        { user_id: "u_blair", label: "Blair", online: 1, total: 1 },
        { user_id: null, label: "Unattributed", online: 1, total: 1 },
      ],
      unattributed_count: 1,
      total_agents: 2,
    });
    renderLive("/live?user=missing");
    expect(await screen.findByText("No agents for this person on this project.")).toBeInTheDocument();
    expect(screen.queryByText("No agents have registered on this project.")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Unattributed/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Blair/ })).toBeInTheDocument();
  });

  it("keeps Unattributed on the census when a named user is filtered", async () => {
    board = emptyBoard({
      total_agents: 2,
      unattributed_count: 1,
      users: [user()],
      user_counts: [
        { user_id: "u_blair", label: "Blair", online: 1, total: 1 },
        { user_id: null, label: "Unattributed", online: 1, total: 1 },
      ],
    });
    renderLive("/live?user=u_blair");
    expect(await screen.findByRole("heading", { name: "Blair" })).toBeInTheDocument();
    const una = screen.getByRole("link", { name: /Unattributed/ });
    expect(una).toHaveAttribute("href", "/live?user=unattributed");
    expect(screen.getByRole("link", { name: /^All/ })).toHaveAttribute("href", "/live");
  });

  it("renders unreserved as a lease miss, not idle, even with empty files", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({
        agents: [agent({
          file_state: "unreserved",
          files: [],
          holdings: [{
            id: "CORE-1", title: "claim-next work", status: "in_progress",
            phase: "building", phase_basis: "status",
            pr: { state: "unrecorded" },
          }],
        })],
      })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    renderLive();
    expect(await screen.findByText("holds work with no area lease")).toBeInTheDocument();
    expect(screen.queryByText(/^idle$/)).not.toBeInTheDocument();
    expect(screen.getByText("unrecorded")).toBeInTheDocument();
    expect(screen.queryByText(/no PRs/i)).not.toBeInTheDocument();
    expect(screen.getByText("Recorded PRs")).toBeInTheDocument();
  });

  it("omits Recorded PRs on an idle agent and labels predicted files", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({
        agents: [agent({
          file_state: "predicted",
          files: [{ area: "web/src", kind: "predicted", reason: null, node_paths: [] }],
          holdings: [],
        })],
      })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    renderLive();
    expect(await screen.findByText("web/src")).toBeInTheDocument();
    expect(screen.getAllByText("predicted").length).toBeGreaterThan(0);
    expect(screen.queryByText("Recorded PRs")).not.toBeInTheDocument();
    expect(screen.queryByText("unrecorded")).not.toBeInTheDocument();
  });

  it("shows offline agents faded and states truncation as N of M", async () => {
    board = emptyBoard({
      truncated: true,
      total_agents: 40,
      users: [user({
        online: 0,
        agents: [agent({ state: "offline", file_state: "offline", label: "gone-one" })],
      })],
      user_counts: [
        { user_id: "u_blair", label: "Blair", online: 0, total: 1 },
      ],
    });
    renderLive();
    expect(await screen.findByText(/Showing .* of 40 agents/)).toBeInTheDocument();
    const gone = await screen.findByText("gone-one");
    expect(gone.closest("div[class*='opacity']")).toBeTruthy();
  });

  it("renders a recorded PR URL and keeps Unattributed as a first-class group", async () => {
    board = emptyBoard({
      total_agents: 2,
      unattributed_count: 1,
      users: [
        user({
          agents: [agent({
            file_state: "leased",
            files: [{ area: "backend/app", kind: "leased", reason: null, node_paths: ["backend/app/x.py"] }],
            holdings: [{
              id: "CORE-9", title: "ship it", status: "review",
              phase: "reviewing", phase_basis: "status",
              pr: { state: "recorded", url: "https://github.com/acme/x/pull/9" },
            }],
          })],
        }),
        user({
          user_id: null,
          label: "Unattributed",
          initials: "",
          color: null,
          online: 1,
          total: 1,
          agents: [agent({ id: "a-orphan", label: "orphan-key", file_state: "idle" })],
        }),
      ],
      user_counts: [
        { user_id: "u_blair", label: "Blair", online: 1, total: 1 },
        { user_id: null, label: "Unattributed", online: 1, total: 1 },
      ],
    });
    renderLive();
    expect(await screen.findByRole("heading", { name: "Unattributed" })).toBeInTheDocument();
    expect(screen.getByText("orphan-key")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "https://github.com/acme/x/pull/9" }))
      .toHaveAttribute("href", "https://github.com/acme/x/pull/9");
  });

  it("labels declared files and does not call them leased, and links Fleet", async () => {
    board = emptyBoard({
      total_agents: 1,
      by_role: { worker: 1, planner: 0, reviewer: 0, "all-in-one": 0 },
      users: [user({
        agents: [agent({
          file_state: "unreserved",
          files: [{ area: "web/src/live.ts", kind: "declared", reason: null, node_paths: [] }],
          holdings: [{
            id: "CORE-1", title: "claim-next work", status: "in_progress",
            phase: "building", phase_basis: "status",
            pr: { state: "unrecorded" },
          }],
        })],
      })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    renderLive();
    expect(await screen.findByText("holds work with no area lease")).toBeInTheDocument();
    expect(screen.getByText("declared on item, not reserved")).toBeInTheDocument();
    expect(screen.queryByText(/^leased$/)).not.toBeInTheDocument();
    expect(screen.getByText("1 worker")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Fleet" })).toHaveAttribute("href", "/fleet");
  });

  // ---- PRD-34 PR 1: the feed. Silence is a word; sources are named; nothing is guessed. ----

  it("says no calls recorded and no status reported, never a blank", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({ call_state: "never", last_call: null, status_state: "unreported" })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    renderLive();
    expect(await screen.findByText("no calls recorded")).toBeInTheDocument();
    expect(screen.getByText("no status reported")).toBeInTheDocument();
  });

  it("shows the last observed call and how long the agent has been quiet", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({
        call_state: "quiet",
        silence_seconds: 720,
        calls_in_window: 0,
        last_call: { tool: "search_code", target: "reservation lease", at: "2026-09-02T11:48:10Z", ok: true },
      })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    renderLive();
    expect(await screen.findByText("search_code")).toBeInTheDocument();
    expect(screen.getByText("reservation lease")).toBeInTheDocument();
    expect(screen.getByText(/no calls for 12m/)).toBeInTheDocument();
    expect(screen.getByText("observed")).toBeInTheDocument();
    expect(screen.queryByText("no calls recorded")).not.toBeInTheDocument();
  });

  it("renders a reported status with its age and marks a stale one", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({
        status_state: "stale",
        status: { text: "running the backend suite", files: ["backend/tests/test_live.py"], at: "2026-09-02T11:00:00Z", stale: true },
      })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    renderLive();
    expect(await screen.findByText("running the backend suite")).toBeInTheDocument();
    expect(screen.getByText("reported")).toBeInTheDocument();
    expect(screen.getByText("stale")).toBeInTheDocument();
    expect(screen.queryByText("no status reported")).not.toBeInTheDocument();
  });

  it("counts unattributed calls on the credential, by key name, without inventing an agent", async () => {
    board = emptyBoard({
      total_agents: 0,
      users: [user({ total: 0, online: 0, agents: [], unattributed_calls: 3,
        unattributed_by_key: [{ key: "feed-key", calls: 3 }] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 0, total: 0 }],
    });
    renderLive();
    expect(await screen.findByText(/3 calls on credential/)).toBeInTheDocument();
    expect(screen.getByText("feed-key")).toBeInTheDocument();
    expect(screen.queryByText("unnamed agent")).not.toBeInTheDocument();
  });

  it("expands a row into its feed, with observed and reported rows marked differently", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({
        call_state: "active", silence_seconds: 3, calls_in_window: 4,
        last_call: { tool: "get_item_details", target: "CORE-9", at: "2026-09-02T12:00:07Z", ok: true },
      })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    feed = {
      served_at: "2026-09-02T12:00:10Z", retention_days: 7, state: "ok",
      rows: [
        { id: 3, at: "2026-09-02T12:00:07Z", source: "observed", tool: "get_item_details", target: "CORE-9", ok: true, write: false },
        { id: 2, at: "2026-09-02T12:00:01Z", source: "reported", tool: "heartbeat", target: "", ok: true, write: true, status: "editing the router", files: ["backend/app/routers/live.py"] },
        { id: 1, at: "2026-09-02T11:59:50Z", source: "observed", tool: "sign_off", target: "CORE-8", ok: false, write: true, error_code: "conflict" },
      ],
    };
    renderLive();
    const toggle = await screen.findByRole("button", { expanded: false });
    fireEvent.click(toggle);
    const list = await screen.findByRole("list", { name: "feed" });
    expect(list).toBeInTheDocument();
    expect(screen.getByText("editing the router")).toBeInTheDocument();
    expect(screen.getByText("backend/app/routers/live.py")).toBeInTheDocument();
    expect(screen.getByText("sign_off")).toBeInTheDocument();
    expect(screen.getByText("conflict")).toBeInTheDocument();
    // The reported row carries the mark; the observed rows do not.
    expect(screen.getAllByText("reported")).toHaveLength(1);
  });

  it("says the feed is empty in words when the state is never", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent()] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    feed = { served_at: "2026-09-02T12:00:10Z", retention_days: 7, state: "never", rows: [] };
    renderLive();
    fireEvent.click(await screen.findByRole("button", { expanded: false }));
    expect(await screen.findByText("No calls recorded in the last 7 days.")).toBeInTheDocument();
  });

  it("labels reported files as a claim, not a lease, and leaves file_state alone", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({
        file_state: "unreserved",
        files: [{ area: "backend/app/routers/live.py", kind: "reported", reason: null, node_paths: [] }],
        holdings: [{ id: "CORE-1", title: "w", status: "in_progress", phase: "building", phase_basis: "status", pr: { state: "unrecorded" } }],
        status_state: "reported",
        status: { text: "editing the router", files: ["backend/app/routers/live.py"], at: "2026-09-02T12:00:05Z", stale: false },
      })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    renderLive();
    expect(await screen.findByText("holds work with no area lease")).toBeInTheDocument();
    expect(screen.getByText("reported by agent, not reserved")).toBeInTheDocument();
    expect(screen.queryByText(/^leased$/)).not.toBeInTheDocument();
    expect(screen.getByText("editing the router")).toBeInTheDocument();
    expect(screen.queryByText("stale")).not.toBeInTheDocument();
  });

  // ---- PRD-34 PR 3: polish that adds no sources ----

  function feedBoard() {
    return emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({ call_state: "active", silence_seconds: 1, calls_in_window: 6,
        last_call: { tool: "get_context", target: "CORE-9", at: "2026-09-02T12:00:09Z", ok: true } })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
  }

  it("folds a run of identical observed calls into one row with a count, and keeps a break in the run", async () => {
    board = feedBoard();
    feed = {
      served_at: "2026-09-02T12:00:10Z", retention_days: 7, state: "ok",
      rows: [
        { id: 6, at: "2026-09-02T12:00:09Z", source: "observed", tool: "get_context", target: "CORE-9", ok: true, write: false },
        { id: 5, at: "2026-09-02T12:00:08Z", source: "observed", tool: "get_context", target: "CORE-9", ok: true, write: false },
        { id: 4, at: "2026-09-02T12:00:07Z", source: "observed", tool: "get_context", target: "CORE-9", ok: true, write: false },
        { id: 3, at: "2026-09-02T12:00:06Z", source: "observed", tool: "search_code", target: "lease", ok: true, write: false },
        { id: 2, at: "2026-09-02T12:00:05Z", source: "observed", tool: "get_context", target: "CORE-9", ok: true, write: false },
        { id: 1, at: "2026-09-02T12:00:04Z", source: "observed", tool: "get_context", target: "CORE-7", ok: true, write: false },
      ],
    };
    renderLive();
    fireEvent.click(await screen.findByRole("button", { expanded: false }));
    const list = await screen.findByRole("list", { name: "feed" });
    // 6 rows -> 4 runs: ×3, search_code, CORE-9 again (a different run), CORE-7 (different target).
    expect(list.querySelectorAll("li")).toHaveLength(4);
    expect(screen.getByLabelText("3 calls")).toHaveTextContent("×3");
    // Scoped to the list: the row summary above it also names the last call.
    expect(within(list).getAllByText("get_context")).toHaveLength(3);
  });

  it("filters the open feed by reads, writes and failures, using the server's write flag", async () => {
    board = feedBoard();
    feed = {
      served_at: "2026-09-02T12:00:10Z", retention_days: 7, state: "ok",
      rows: [
        { id: 3, at: "2026-09-02T12:00:09Z", source: "observed", tool: "search_code", target: "lease", ok: true, write: false },
        { id: 2, at: "2026-09-02T12:00:08Z", source: "observed", tool: "update_item", target: "CORE-9 → review", ok: true, write: true },
        { id: 1, at: "2026-09-02T12:00:07Z", source: "observed", tool: "sign_off", target: "CORE-8", ok: false, write: true, error_code: "conflict" },
      ],
    };
    renderLive();
    fireEvent.click(await screen.findByRole("button", { expanded: false }));
    await screen.findByRole("list", { name: "feed" });
    fireEvent.click(screen.getByRole("button", { name: "reads" }));
    expect(screen.getByText("search_code")).toBeInTheDocument();
    expect(screen.queryByText("update_item")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "writes" }));
    expect(screen.getByText("update_item")).toBeInTheDocument();
    expect(screen.getByText("sign_off")).toBeInTheDocument();
    expect(screen.queryByText("search_code")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "failures" }));
    expect(screen.getByText("sign_off")).toBeInTheDocument();
    expect(screen.queryByText("update_item")).not.toBeInTheDocument();
    // A failed call on an item links to the tracker.
    expect(screen.getByRole("link", { name: "CORE-8" })).toHaveAttribute("href", "/tracker");
  });

  it("says in words when a filter matches nothing, rather than an empty list", async () => {
    board = feedBoard();
    feed = {
      served_at: "2026-09-02T12:00:10Z", retention_days: 7, state: "ok",
      rows: [{ id: 1, at: "2026-09-02T12:00:09Z", source: "observed", tool: "search_code", target: "lease", ok: true, write: false }],
    };
    renderLive();
    fireEvent.click(await screen.findByRole("button", { expanded: false }));
    await screen.findByRole("list", { name: "feed" });
    fireEvent.click(screen.getByRole("button", { name: "failures" }));
    expect(screen.getByText("No failures in this feed.")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "feed" })).not.toBeInTheDocument();
  });
});
