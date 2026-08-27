import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { TriageView } from "@/features/triage/TriageView";
import type { Project, TriageRow } from "@/lib/types";

/**
 * PRD-21 screen 14.
 *
 * The assertions that matter are about the two ways this screen can lie: a duplicate
 * hint that reads as a decision rather than advice, and a green "all clear" over a
 * project where nothing is claimed at all.
 */
const core: Project = {
  id: "prj_core", name: "Core", tag: "CORE", accent: "#c6f24e", visibility: "private",
  description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
  embed_model: "",
  credential_id: null,
  model_override: "", memory_auto_reject: true, memory_write_mode: "review",
  memory_llm_judge: false, agent_adjudication: false, allow_self_review: false,
};

const req = (id: string, title: string, extra: Partial<TriageRow["request"]> = {}) => ({
  id, project_id: "prj_core", type: "bug" as const, title, detail: "", by: "dana",
  votes: 3, status: "new", linked_to: null, ago: "2h", source_url: "", meta: {},
  attachment_ids: [], created_at: "", ...extra,
});

const QUEUE: TriageRow[] = [
  { request: req("CORE-R1", "Crash on empty project"), duplicate: null },
  {
    request: req("CORE-R2", "Sidebar collapses on resize"),
    duplicate: { kind: "request", id: "CORE-R7", title: "Sidebar collapse bug", score: 0.86 },
  },
];

const { acceptSpy } = vi.hoisted(() => ({ acceptSpy: vi.fn(async () => ({})) }));

const fleet = (clusters: unknown[]) => ({
  agents: [], online: 0, total: 0, by_role: {}, posture: "single-agent", roles: [],
  presence_ttl_seconds: 90, heartbeat_interval_seconds: 30, review_queue: [],
  clusters, seats: [], waves: [],
});

vi.mock("@/lib/api", () => ({
  api: {
    projects: vi.fn(async () => [core]),
    triageQueue: vi.fn(async () => QUEUE),
    acceptRequest: acceptSpy,
    voteRequest: vi.fn(async () => ({})),
    fleet: vi.fn(async () => fleet([])),
  },
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

describe("Triage", () => {
  beforeEach(async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.fleet).mockResolvedValue(fleet([]) as never);
  });

  it("labels itself per-project, because clustering never crosses repos", async () => {
    renderTriage();
    expect(await screen.findByText(/per-project · CORE/)).toBeInTheDocument();
    expect(screen.getByText(/a shared package name is a galaxy edge, not a collision/))
      .toBeInTheDocument();
  });

  it("shows a duplicate hint as advice, on the row that has one", async () => {
    renderTriage();
    expect(await screen.findByText(/looks like/)).toBeInTheDocument();
    expect(screen.getByText("CORE-R7")).toBeInTheDocument();
    expect(screen.getByText(/86% similar/)).toBeInTheDocument();
    // The row without a match shows nothing rather than an empty hint box — a null
    // duplicate means "compared, nothing matched", not "unknown".
    expect(screen.getAllByText(/looks like/)).toHaveLength(1);
  });

  it("accepts a request into tracked work", async () => {
    renderTriage();
    const buttons = await screen.findAllByRole("button", { name: "Accept" });
    await userEvent.click(buttons[0]);
    expect(acceptSpy).toHaveBeenCalledWith("CORE-R1");
  });

  it("distinguishes nothing-in-flight from nothing-colliding", async () => {
    // The failure this guards: a calm green "all clear" over a project where nobody is
    // working. The check found no work, not no conflict, and those read identically
    // unless the empty state says which.
    renderTriage();
    expect(await screen.findByText("Nothing in flight")).toBeInTheDocument();
    expect(screen.getByText(/idle project, not a cleared one/)).toBeInTheDocument();
  });

  it("says all-clear only when claims exist and none overlap", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.fleet).mockResolvedValue(
      fleet([
        { items: ["CORE-1"], areas: ["a.py"], predicted: false, held_by: "x", blocked_on: null },
        { items: ["CORE-2"], areas: ["b.py"], predicted: false, held_by: "y", blocked_on: null },
      ]) as never,
    );
    renderTriage();
    expect(await screen.findByText("No overlaps in flight")).toBeInTheDocument();
    expect(screen.getByText(/2 claims open and no two touch the same code/)).toBeInTheDocument();
  });

  it("draws a cluster only where two items actually share code", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.fleet).mockResolvedValue(
      fleet([
        {
          items: ["CORE-4", "CORE-9"], areas: ["services/code_graph.py"],
          predicted: true, held_by: "agent-a", blocked_on: null,
        },
      ]) as never,
    );
    renderTriage();
    expect(await screen.findByText("2 items overlap")).toBeInTheDocument();
    expect(screen.getByText("serialize")).toBeInTheDocument();
    expect(screen.getByText("predicted")).toBeInTheDocument();
    // Twice on purpose: once as a chip in the shared-paths list, once inside the
    // recommendation, so the sentence names the file rather than saying "the same code".
    expect(screen.getAllByText("services/code_graph.py")).toHaveLength(2);
  });
});
