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
  answerBlocker: vi.fn(async () => ({})),
  decideCollision: vi.fn(async () => ({ id: 7 })),
  undoCollisionDecision: vi.fn(async () => ({})),
  requestComments: vi.fn(),
  createRequestComment: vi.fn(async () => ({ id: "rcom_new", body: "", visibility: "public", created_at: "" })),
  me: vi.fn(async () => ({ id: "u1", name: "Alex Smith", handle: "alex", email: "", avatar: "", initials: "AS" })),
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
  m.requestComments.mockResolvedValue([]);
});

describe("Triage inbox", () => {
  it("lists the queue with the first row selected and counts on the tabs", async () => {
    renderTriage();
    expect(await screen.findByText("Decider falls back silently", { selector: "span" })).toBeInTheDocument();
    expect(selected()).toHaveTextContent("Decider falls back silently");
    expect(screen.getByText("4", { selector: "span.text-fg" })).toBeInTheDocument();
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

  it("answers a decision's third option on its own letter", async () => {
    const decide: TriageFeedRow = {
      ...blocker, id: "GRPH-906",
      decision: { question: "Which store?", options: ["Postgres", "SQLite", "Both", "Neither"] },
    };
    m.projectTriage.mockResolvedValue(feed([decide, bug]));
    renderTriage();
    await screen.findByText("Crash on empty project");
    // Option keys are A B D F G H — C is the comment box, so the third option is D.
    const user = userEvent.setup();
    await user.keyboard("c");
    expect(m.answerBlocker).not.toHaveBeenCalled();
    await user.keyboard("d");
    await waitFor(() => expect(m.answerBlocker).toHaveBeenCalledWith("GRPH-906", "Both"));
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
    expect(screen.getByText(/Nothing waiting\. Everything reported has been triaged/)).toBeInTheDocument();
  });

  it("puts the tab row above both columns, not inside the list", async () => {
    renderTriage();
    await screen.findByText("Crash on empty project");
    const tabs = screen.getByRole("group", { name: "Triage tabs" });
    expect(tabs.contains(screen.getByRole("button", { name: /Collisions/ }))).toBe(true);
    expect(tabs.contains(screen.getByRole("listbox"))).toBe(false);
    expect(screen.getByRole("listbox").closest("[role=group]")).toBeNull();
  });

  it("hides the Processed card until something is processed", async () => {
    renderTriage();
    await screen.findByText("Crash on empty project");
    expect(screen.queryByRole("button", { name: /Undo last/ })).not.toBeInTheDocument();
  });

  it("renders the blocker callout with the agent's options as chips", async () => {
    m.projectTriage.mockResolvedValue(feed([
      { ...blocker, decision: { question: "Which key?", options: ["Use env", "Use vault"] } } as TriageFeedRow,
    ]));
    renderTriage();
    const label = await screen.findByText("AGENT ASKED");
    expect(label.parentElement).toHaveClass("border-st-blocked/30");
    expect(screen.getByText("Which key?")).toBeInTheDocument();
    const chips = screen.getByLabelText("Agent's options");
    expect(chips).toHaveTextContent("Use env");
    expect(chips).toHaveTextContent("Use vault");
    expect(document.querySelector("ul")).toBeNull();
  });

  it("renders the collision cluster table and the amber recommendation callout", async () => {
    m.projectTriage.mockResolvedValue(feed([collision]));
    renderTriage();
    expect(await screen.findByText("ITEMS IN THIS CLUSTER")).toBeInTheDocument();
    expect(screen.queryByText("MEMBERS, IN ORDER")).not.toBeInTheDocument();
    const rec = screen.getByText("RECOMMENDATION · BLOCKED");
    expect(rec.parentElement).toHaveClass("border-st-review/30");
    expect(screen.getByText("Hold the rest.")).toBeInTheDocument();
    expect(screen.getByText("GRPH-2")).toBeInTheDocument();
  });

  it("renders the likely-duplicate callout with the score in its heading", async () => {
    m.projectTriage.mockResolvedValue(feed([dupBug]));
    renderTriage();
    const head = await screen.findByText(/LIKELY DUPLICATE · 86%/);
    expect(head.parentElement).toHaveClass("border-st-next/30");
    expect(screen.getByText("CORE-R7")).toBeInTheDocument();
    expect(screen.getByText("Sidebar collapse bug")).toBeInTheDocument();
  });
});

/** GRPH-1149, PRD-60 D9: the comment thread on a request in the detail pane. */
describe("Triage request comments", () => {
  const thread = [
    { id: "c1", body: "Owner = whoever's key it runs under.", visibility: "public", role: "submitter", author: "dana", created_at: "2026-10-28T10:00:00Z" },
    { id: "c2", body: "Any preference on the key?", visibility: "public", role: "team", author: "Alex Smith", created_at: "2026-10-28T11:00:00Z" },
    { id: "c3", body: "Overlaps PRD-47 — fold in there?", visibility: "private", role: "internal", author: "Kim Lee", created_at: "2026-10-28T12:00:00Z" },
  ];

  async function openBug() {
    m.projectTriage.mockResolvedValue(feed([bug]));
    renderTriage();
    return screen.findByRole("textbox", { name: "Comment" });
  }

  it("lists comments with role tags, 'You' for self, and internal notes in amber", async () => {
    m.requestComments.mockResolvedValue(thread);
    await openBug();
    expect(await screen.findByText(/Comments · 3/)).toBeInTheDocument();
    expect(m.requestComments).toHaveBeenCalledWith("CORE-R1");
    expect(screen.getByText("SUBMITTER")).toBeInTheDocument();
    expect(screen.getByText("TEAM")).toBeInTheDocument();
    expect(screen.getByText("You")).toBeInTheDocument();
    const note = document.querySelector('[data-role="internal"]')!;
    expect(note).toHaveTextContent("INTERNAL");
    expect(note.querySelector(".border-st-review\\/25")).not.toBeNull();
    expect(document.querySelector('[data-role="team"] .border-st-review\\/25')).toBeNull();
  });

  it("shows the empty copy when there are no comments", async () => {
    await openBug();
    expect(await screen.findByText(/No comments yet. Ask the submitter for detail, or leave a note for the team./)).toBeInTheDocument();
  });

  it("C focuses the composer, and letter keys are inert while it has focus", async () => {
    const box = await openBug();
    const user = userEvent.setup();
    await user.keyboard("c");
    expect(box).toHaveFocus();
    await user.keyboard("ad");
    expect(box).toHaveValue("ad");
    expect(m.acceptRequestTo).not.toHaveBeenCalled();
    expect(m.mergeRequest).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    expect(box).not.toHaveFocus();
  });

  it("⌘↵ posts a public reply that notifies the submitter, and never mentions voters", async () => {
    const box = await openBug();
    expect(screen.getByText("notifies the submitter")).toBeInTheDocument();
    expect(screen.queryByText(/voter/i)).not.toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(box);
    await user.keyboard("{Meta>}{Enter}{/Meta}");
    expect(m.createRequestComment).not.toHaveBeenCalled(); // empty draft cannot post
    expect(screen.getByRole("button", { name: /Comment/ })).toBeDisabled();
    await user.keyboard("Thanks, on it");
    await user.keyboard("{Meta>}{Enter}{/Meta}");
    await waitFor(() => expect(m.createRequestComment).toHaveBeenCalledWith("CORE-R1", "Thanks, on it", "public"));
    await waitFor(() => expect(box).toHaveValue(""));
  });

  it("an internal note posts private, with its own hint and an amber box", async () => {
    const box = await openBug();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Internal note" }));
    expect(screen.getByText("only your team sees this")).toBeInTheDocument();
    expect(box).toHaveAttribute("placeholder", "Note for the team — not visible to the submitter");
    expect(box.parentElement).toHaveClass("border-st-review/30");
    await user.click(box);
    await user.keyboard("   ");
    await user.keyboard("{Meta>}{Enter}{/Meta}");
    expect(m.createRequestComment).not.toHaveBeenCalled(); // whitespace is not a draft
    await user.keyboard("dup of PRD-47");
    await user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(m.createRequestComment).toHaveBeenCalledWith("CORE-R1", "dup of PRD-47", "private"));
  });

  it("does not mount a thread on blockers", async () => {
    m.projectTriage.mockResolvedValue(feed([blocker]));
    renderTriage();
    await screen.findByText("Needs the key API");
    expect(screen.queryByRole("textbox", { name: "Comment" })).not.toBeInTheDocument();
    expect(m.requestComments).not.toHaveBeenCalled();
  });
});
