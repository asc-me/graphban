import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LiveView } from "@/features/live/LiveView";
import { ProjectProvider } from "@/features/ProjectContext";
import type { FleetOverview, LiveAgent, LiveBoard, LiveDelegationRow, LiveFeed, LiveUser } from "@/lib/types";

const project = {
  id: "core", tag: "CORE", name: "Core", accent: "#c6f24e", visibility: "private",
  description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
  embed_model: "", credential_id: null, model_override: "",
  memory_auto_reject: true, memory_write_mode: "review", memory_llm_judge: false,
  agent_adjudication: false, allow_self_review: false,
};

const emptyFleet = (): FleetOverview => ({
  agents: [], online: 0, total: 0, by_role: {}, posture: "single-agent", roles: [],
  presence_ttl_seconds: 150, heartbeat_interval_seconds: 50, review_queue: [], clusters: [],
  seats: [], credentials: [], waves: [], profile: null, policy: null, measured: [],
});

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
let fleet: FleetOverview = emptyFleet();
let feed: LiveFeed = { served_at: "2026-09-02T12:00:10Z", retention_days: 7, state: "never", rows: [] };
let livePending = false;
let releaseLive: ((value: LiveBoard) => void) | null = null;

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [project]),
    config: vi.fn(async () => ({ hosted_mode: false, signup_mode: "closed" })),
    live: vi.fn(async () => {
      if (livePending) {
        return new Promise<LiveBoard>((resolve) => {
          releaseLive = resolve;
        });
      }
      return board;
    }),
    liveFeed: vi.fn(async () => feed),
    fleet: vi.fn(async () => fleet),
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
    fleet = emptyFleet();
    feed = { served_at: "2026-09-02T12:00:10Z", retention_days: 7, state: "never", rows: [] };
    livePending = false;
    releaseLive = null;
  });

  afterEach(() => {
    releaseLive?.(emptyBoard());
    releaseLive = null;
    livePending = false;
  });

  it("shows a loading skeleton with the page header instead of centred Loading text (GRPH-919)", async () => {
    livePending = true;
    renderLive();
    expect(screen.getByRole("heading", { name: "Live" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Loading live board")).toBeInTheDocument());
    expect(screen.queryByText(/^Loading/)).not.toBeInTheDocument();
    releaseLive?.(emptyBoard());
    livePending = false;
  });

  it("names an empty project as unregistered, not idle", async () => {
    renderLive();
    expect(await screen.findByRole("heading", { name: "Live" })).toBeInTheDocument();
    expect(await screen.findByText("No agents on this project yet")).toBeInTheDocument();
    expect(screen.queryByText(/idle/i)).not.toBeInTheDocument();
  });

  it("empty state names what the view is for and offers a next move (GRPH-939)", async () => {
    renderLive();
    expect(await screen.findByText("No agents on this project yet")).toBeInTheDocument();
    expect(screen.getByText(/Live shows who is working here right now/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open Fleet\.v1/ })).toBeInTheDocument();
  });

  it("names a filter miss as a person, not an empty project", async () => {
    board = emptyBoard({
      user_counts: [
        { user_id: "u_blair", label: "Blair", online: 1, total: 1 },
      ],
      total_agents: 1,
    });
    renderLive("/live?user=missing");
    expect(await screen.findByText("No agents for this person")).toBeInTheDocument();
    expect(screen.queryByText("No agents on this project yet")).not.toBeInTheDocument();
  });

  it("shows truncation as N of M agents", async () => {
    board = emptyBoard({
      truncated: true,
      total_agents: 40,
      users: [user({ agents: [agent({ label: "gone-one", state: "offline", file_state: "offline" })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 0, total: 1 }],
    });
    renderLive();
    expect(await screen.findByText(/Showing .* of 40 agents/)).toBeInTheDocument();
  });

  it("renders the S11 KPI row from board facts (GRPH-962)", async () => {
    board = emptyBoard({
      total_agents: 2,
      window_seconds: 600,
      users: [user({
        online: 2,
        total: 2,
        agents: [
          agent({ id: "w1", label: "worker-one", calls_in_window: 6 }),
          agent({ id: "p1", label: "planner-one", role: "planner", calls_in_window: 2,
            last_call: { tool: "delegate", target: "CORE-9", at: "2026-09-02T12:00:09Z", ok: true } }),
        ],
      })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 2, total: 2 }],
    });
    renderLive();
    expect(await screen.findByText("Online")).toBeInTheDocument();
    expect(screen.getByText("Touches / min")).toBeInTheDocument();
    expect(screen.getByText("Planner calls")).toBeInTheDocument();
    expect(screen.getByText("0.8")).toBeInTheDocument();
  });

  it("renders a presence strip chip per agent and focuses the stream on click (GRPH-962)", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({ id: "a-focus", label: "focus-me", call_state: "active",
        last_call: { tool: "heartbeat", target: "", at: "2026-09-02T12:00:09Z", ok: true } })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    feed = {
      served_at: "2026-09-02T12:00:10Z", retention_days: 7, state: "ok",
      rows: [{ id: 1, at: "2026-09-02T12:00:09Z", source: "observed", tool: "heartbeat", target: "", ok: true, write: false }],
    };
    renderLive();
    expect(await screen.findByRole("list", { name: "Agent presence" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("focus-me"));
    expect(await screen.findByText("Filtered to the focused agent.")).toBeInTheDocument();
  });

  it("shows planner delegations on the planner desk (PRD-35)", async () => {
    const row = (over: Partial<LiveDelegationRow>): LiveDelegationRow => ({
      id: "dlg", item: "CORE-9", state: "open", lane: "backend", requested_tier: "cheap",
      declared_tier: null, declared_model: null, mismatch: false, delegated_by: "a2",
      agent_id: null, linked_by: null, outcome: null, closed_reason: null, closed_by: null,
      note: "", created_at: null, claimed_at: null, age_seconds: 700, ...over,
    });
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({
        id: "a2", key: "CORE-A2", label: "planner-two", role: "planner",
        delegations: {
          open: 0, claimed: 1, finished: 0, expired: 1, closed: 0, oldest_open_seconds: null,
          rows: [
            row({ id: "d1", item: "CORE-9", state: "expired" }),
            row({ id: "d2", item: "CORE-10", state: "claimed", agent_id: "CORE-A7" }),
          ],
        },
      })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    renderLive();
    const desk = await screen.findByText("Planner desk");
    expect(within(desk.closest("section")!).getByText("planner-two")).toBeInTheDocument();
    expect(within(desk.closest("section")!).getByText("CORE-10")).toBeInTheDocument();
  });

  it("names held-back clusters with the colliding area and who holds it (GRPH-962)", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({ role: "planner", label: "solo-planner" })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    fleet = {
      ...emptyFleet(),
      clusters: [{
        items: ["CORE-1", "CORE-2"], areas: ["web/src/features/live/"], predicted: false,
        held_by: "CORE-A9", blocked_on: "web/src/features/live/",
      }],
    };
    renderLive();
    expect(await screen.findByText("Held back")).toBeInTheDocument();
    expect(screen.getByText(/held by CORE-A9/)).toBeInTheDocument();
    expect(screen.getByText(/collides on web\/src\/features\/live\//)).toBeInTheDocument();
  });

  it("renders ticket lanes for held items (GRPH-962)", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({
        id: "w1", label: "builder",
        holdings: [{ id: "CORE-9", title: "ship live", status: "in_progress", phase: "building", phase_basis: "x", pr: { state: "unrecorded" } }],
      })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    renderLive();
    expect(await screen.findByText("Tickets in motion")).toBeInTheDocument();
    expect(screen.getByText("CORE-9")).toBeInTheDocument();
    expect(screen.getByText("ship live")).toBeInTheDocument();
  });

  it("pause freezes the displayed board until resume (GRPH-962)", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({ label: "before-pause" })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    renderLive();
    expect(await screen.findByText("before-pause")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
  });

  it("shows the stream with observed and reported rows marked differently", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({ id: "a-feed", call_state: "active" })] })],
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
    const list = await screen.findByRole("list", { name: "stream" });
    expect(list).toBeInTheDocument();
    expect(screen.getByText("editing the router")).toBeInTheDocument();
    expect(screen.getByText("sign_off")).toBeInTheDocument();
    expect(screen.getByText("conflict")).toBeInTheDocument();
    expect(screen.getAllByText("reported")).toHaveLength(1);
  });

  it("says the stream is empty in words when the state is never", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent()] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    feed = { served_at: "2026-09-02T12:00:10Z", retention_days: 7, state: "never", rows: [] };
    renderLive();
    expect(await screen.findByText(/No calls recorded in the last 7 days/)).toBeInTheDocument();
  });

  it("folds a run of identical observed calls into one row with a count", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({ id: "a-feed" })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    feed = {
      served_at: "2026-09-02T12:00:10Z", retention_days: 7, state: "ok",
      rows: [
        { id: 3, at: "2026-09-02T12:00:09Z", source: "observed", tool: "get_context", target: "CORE-9", ok: true, write: false },
        { id: 2, at: "2026-09-02T12:00:08Z", source: "observed", tool: "get_context", target: "CORE-9", ok: true, write: false },
        { id: 1, at: "2026-09-02T12:00:07Z", source: "observed", tool: "get_context", target: "CORE-9", ok: true, write: false },
      ],
    };
    renderLive();
    const list = await screen.findByRole("list", { name: "stream" });
    expect(list.querySelectorAll("li")).toHaveLength(1);
    expect(screen.getByLabelText("3 calls")).toHaveTextContent("×3");
  });

  it("filters the stream by reads, writes, planner and failures", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({ id: "a-feed" })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    feed = {
      served_at: "2026-09-02T12:00:10Z", retention_days: 7, state: "ok",
      rows: [
        { id: 4, at: "2026-09-02T12:00:09Z", source: "observed", tool: "search_code", target: "lease", ok: true, write: false },
        { id: 3, at: "2026-09-02T12:00:09Z", source: "observed", tool: "update_item", target: "CORE-9", ok: true, write: true },
        { id: 2, at: "2026-09-02T12:00:08Z", source: "observed", tool: "delegate", target: "CORE-9", ok: true, write: true },
        { id: 1, at: "2026-09-02T12:00:07Z", source: "observed", tool: "sign_off", target: "CORE-8", ok: false, write: true, error_code: "conflict" },
      ],
    };
    renderLive();
    await screen.findByRole("list", { name: "stream" });
    fireEvent.click(screen.getByRole("button", { name: "reads" }));
    expect(screen.getByText("search_code")).toBeInTheDocument();
    expect(screen.queryByText("update_item")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "writes" }));
    expect(screen.getByText("update_item")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "planner" }));
    expect(screen.getByText("delegate")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "failures" }));
    expect(screen.getByText("sign_off")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "CORE-8" })).toHaveAttribute("href", "/tracker");
  });

  it("says in words when a stream filter matches nothing", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({ id: "a-feed" })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    feed = {
      served_at: "2026-09-02T12:00:10Z", retention_days: 7, state: "ok",
      rows: [{ id: 1, at: "2026-09-02T12:00:09Z", source: "observed", tool: "search_code", target: "lease", ok: true, write: false }],
    };
    renderLive();
    await screen.findByRole("list", { name: "stream" });
    fireEvent.click(screen.getByRole("button", { name: "failures" }));
    expect(screen.getByText("No failures in this stream.")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "stream" })).not.toBeInTheDocument();
  });

  it("presence chips say no calls recorded and no status reported, never a blank", async () => {
    board = emptyBoard({
      total_agents: 1,
      users: [user({ agents: [agent({ call_state: "never", status_state: "unreported" })] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    renderLive();
    expect(await screen.findByText(/no calls recorded/)).toBeInTheDocument();
  });

  it("links Fleet.v1 from the place header", async () => {
    board = emptyBoard({
      total_agents: 1,
      by_role: { worker: 1 },
      users: [user({ agents: [agent()] })],
      user_counts: [{ user_id: "u_blair", label: "Blair", online: 1, total: 1 }],
    });
    renderLive();
    expect(await screen.findByRole("link", { name: "Fleet.v1" })).toHaveAttribute("href", "/fleet.v1");
  });
});

describe("liveUtils sabotage guards (GRPH-962)", () => {
  it("computeKpis returns null-safe touches per minute", async () => {
    const { computeKpis } = await import("@/features/live/liveUtils");
    const b = emptyBoard({ window_seconds: 0 });
    const flat = [{ agent: agent({ calls_in_window: 5 }), userLabel: "x" }];
    expect(computeKpis(b, flat).touchesPerMinute).toBeNull();
  });

  it("matchesStreamFilter treats planner tools separately from writes", async () => {
    const { matchesStreamFilter } = await import("@/features/live/liveUtils");
    const row = { id: 1, at: "", source: "observed" as const, tool: "delegate", target: "X", ok: true, write: true };
    expect(matchesStreamFilter(row, "planner")).toBe(true);
    expect(matchesStreamFilter(row, "reads")).toBe(false);
  });
});
