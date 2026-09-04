import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LiveView } from "@/features/live/LiveView";
import { ProjectProvider } from "@/features/ProjectContext";
import type { LiveAgent, LiveBoard, LiveFeed, LiveUser } from "@/lib/types";

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
        { id: 3, at: "2026-09-02T12:00:07Z", source: "observed", tool: "get_item_details", target: "CORE-9", ok: true },
        { id: 2, at: "2026-09-02T12:00:01Z", source: "reported", tool: "heartbeat", target: "", ok: true, status: "editing the router", files: ["backend/app/routers/live.py"] },
        { id: 1, at: "2026-09-02T11:59:50Z", source: "observed", tool: "sign_off", target: "CORE-8", ok: false, error_code: "conflict" },
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
});
