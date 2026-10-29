import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { TriageView } from "@/features/triage/TriageView";
import type { Project, TriageFeed, TriageFeedRow } from "@/lib/types";

/**
 * GRPH-1148. The assertions that matter are the ways this screen can lie: "Triage clear"
 * while loading or while a source failed, and an action key firing while the user types.
 */
const core: Project = {
  id: "prj_core", name: "Core", tag: "CORE", accent: "#c6f24e", visibility: "private",
  description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
  embed_model: "", credential_id: null, model_override: "", memory_auto_reject: true,
  memory_write_mode: "review", memory_llm_judge: false, agent_adjudication: false,
  allow_self_review: false,
};

const blocker: TriageFeedRow = {
  id: "GRPH-905", kind: "blocker", title: "Decider falls back silently", age_seconds: 7200,
  paths: ["server/decider.py"], owner: "worker-3", blocked_seconds: 7200, reason: "Needs the key API",
  waiting_on: { id: "GRPH-898", title: "Credentials panel", status: "in_progress" },
};
const bug: TriageFeedRow = {
  id: "CORE-R1", kind: "bug", title: "Crash on empty project", age_seconds: 3600, paths: [],
  by: "dana", votes: 3, source: "Public form", body: "It crashed.", duplicate: null,
  suggested_prd: { id: "PRD-46", score: 0.62 },
};
const dupBug: TriageFeedRow = {
  id: "CORE-R2", kind: "bug", title: "Sidebar collapses on resize", age_seconds: 1800, paths: [],
  by: "sam", votes: 5, source: "In-app widget", body: "",
  duplicate: { id: "CORE-R7", title: "Sidebar collapse bug", score: 0.86, status: "new" },
};
const collision: TriageFeedRow = {
  id: "col:abc", kind: "collision", title: "2 items touch the same code", age_seconds: 600,
  paths: ["a.py"], risk: "blocked", predicted: false, recommendation: "Hold the rest.",
  members: [
    { id: "GRPH-1", title: "One", owner: "w1", status: "in_progress" },
    { id: "GRPH-2", title: "Two", owner: null, status: "next" },
  ],
};

const feed = (rows: TriageFeedRow[], failed: string[] = []): TriageFeed => ({
  rows,
  counts: {
    all: rows.length,
    bugs: rows.filter((r) => r.kind === "bug").length,
    requests: 0,
    blockers: rows.filter((r) => r.kind === "blocker").length,
    collisions: rows.filter((r) => r.kind === "collision").length,
  },
  failed_sources: failed,
});

const m = vi.hoisted(() => ({
  projectTriage: vi.fn(),
  acceptRequestTo: vi.fn(async () => ({ item: { id: "CORE-9" }, action_id: "act1" })),
  undoRequestAction: vi.fn(async () => ({})),
  mergeRequest: vi.fn(async () => ({ action_id: "act2" })),
  blockerAction: vi.fn(async () => ({})),
  undoBlocker: vi.fn(async () => ({})),
  decideCollision: vi.fn(async () => ({ id: 7 })),
  undoCollisionDecision: vi.fn(async () => ({})),
}));

vi.mock("@/lib/api", () => ({
  api: { projects: vi.fn(async () => [core]), ...m },
}));

function renderTriage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/p/CORE/triage"]}>
        <ProjectProvider>
          <TriageView />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const selected = () => document.querySelector('[role="option"][aria-selected="true"]');

beforeEach(() => {
  vi.clearAllMocks();
  m.projectTriage.mockResolvedValue(feed([blocker, bug, dupBug, collision]));
});

describe("Triage inbox", () => {
  it("lists the queue with the first row selected and counts on the tabs", async () => {
    renderTriage();
    expect(await screen.findByText("Decider falls back silently", { selector: "span" })).toBeInTheDocument();
    expect(selected()).toHaveTextContent("Decider falls back silently");
    expect(screen.getByText(/4/, { selector: "span.text-fg-2" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Bugs\s*2/ })).toBeInTheDocument();
  });

  it("moves with J/K and switches tabs with 1–5", async () => {
    renderTriage();
    await screen.findByText("Crash on empty project");
    const user = userEvent.setup();
    await user.keyboard("j");
    expect(selected()).toHaveTextContent("Crash on empty project");
    await user.keyboard("k");
    expect(selected()).toHaveTextContent("Decider falls back silently");
    await user.keyboard("2");
    expect(screen.queryByText("Decider falls back silently", { selector: "[role=option] span" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("option")).toHaveLength(2);
    await user.keyboard("5");
    expect(screen.getAllByRole("option")).toHaveLength(1);
    await user.keyboard("3");
    expect(screen.getByText("No requests waiting.")).toBeInTheDocument();
  });

  it("runs an action on its letter and on ↵ for the primary, then selects the next row", async () => {
    renderTriage();
    await screen.findByText("Crash on empty project");
    const user = userEvent.setup();
    await user.keyboard("{Enter}"); // blocker primary: Mark unblocked
    await waitFor(() => expect(m.blockerAction).toHaveBeenCalledWith("GRPH-905", "unblock"));
    await waitFor(() => expect(selected()).toHaveTextContent("Crash on empty project"));
    expect(screen.getAllByRole("option")).toHaveLength(3);
    await user.keyboard("n"); // Accept to Next
    await waitFor(() =>
      expect(m.acceptRequestTo).toHaveBeenCalledWith("CORE-R1", "next", "PRD-46"));
    expect(await screen.findByText(/Accepted → Next as CORE-9 · linked PRD-46/)).toBeInTheDocument();
  });

  it("asks before Merge and Override, ↵ confirms, and nothing else asks", async () => {
    renderTriage();
    await screen.findByText("Sidebar collapses on resize");
    const user = userEvent.setup();
    await user.keyboard("jj"); // dup bug
    await user.keyboard("m");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(m.mergeRequest).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(m.mergeRequest).toHaveBeenCalledWith("CORE-R2", "CORE-R7"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.keyboard("5");
    await user.keyboard("o");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(m.decideCollision).not.toHaveBeenCalled();
    await user.keyboard("h"); // hold: no confirmation
    await waitFor(() =>
      expect(m.decideCollision).toHaveBeenCalledWith("prj_core", ["GRPH-1", "GRPH-2"], "hold", undefined));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("undoes the last action with ⌫ and the row returns to the queue, selected", async () => {
    renderTriage();
    await screen.findByText("Crash on empty project");
    const user = userEvent.setup();
    await user.keyboard("u");
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(3));
    await user.keyboard("{Backspace}");
    await waitFor(() => expect(m.undoBlocker).toHaveBeenCalledWith("GRPH-905"));
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(4));
    expect(selected()).toHaveTextContent("Decider falls back silently");
  });

  it("does not fire action keys while typing in a text field", async () => {
    renderTriage();
    await screen.findByText("Crash on empty project");
    const box = document.createElement("textarea");
    document.body.appendChild(box);
    const user = userEvent.setup();
    box.focus();
    await user.keyboard("ujk{Enter}{Backspace}2");
    expect(m.blockerAction).not.toHaveBeenCalled();
    expect(selected()).toHaveTextContent("Decider falls back silently");
    expect(screen.getAllByRole("option")).toHaveLength(4);
    box.remove();
  });

  it("shows a loading line, never 'Triage clear', while the feed is in flight", async () => {
    m.projectTriage.mockReturnValue(new Promise(() => {}));
    renderTriage();
    expect(await screen.findByText(/Loading the triage queue/)).toBeInTheDocument();
    expect(screen.queryByText("Triage clear")).not.toBeInTheDocument();
  });

  it("names failed sources in a banner and does not call a partial queue clear", async () => {
    m.projectTriage.mockResolvedValue(feed([], ["blockers", "collisions"]));
    renderTriage();
    expect(await screen.findByRole("alert")).toHaveTextContent(/blockers, collisions/);
    expect(screen.queryByText("Triage clear")).not.toBeInTheDocument();
  });

  it("says 'Triage clear' only for a real empty feed", async () => {
    m.projectTriage.mockResolvedValue(feed([]));
    renderTriage();
    expect(await screen.findByText("Triage clear")).toBeInTheDocument();
  });
});
