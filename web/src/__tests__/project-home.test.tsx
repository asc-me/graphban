import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { ProjectHome } from "@/features/projecthome/ProjectHome";
import { consoleHref, orgPath } from "@/lib/routes";
import type { Project } from "@/lib/types";

/**
 * PRD-21 D7, screen 6.
 *
 * D7 adds no capability — the project plane is the existing app. What this screen owes the
 * reader is the half of the dependency picture that is invisible from inside the repo:
 * what depends on THIS project. And, like every other surface here, it must not let an
 * empty code graph read as a project with no structure.
 *
 * PRD-47 S17 adds the states: the counts are placeheld while they are being read, a read
 * that failed is named with a way to retry it, and the dependency panel distinguishes
 * "not read" from "nothing depends on this" — three facts that all used to render as the
 * same silence.
 */
const core: Project = {
  id: "prj_core", name: "Core", tag: "CORE", accent: "#c6f24e", visibility: "private",
  description: "The shared library.", share_global_memory: false, auto_extract: true,
  mcp_enabled: true, embed_model: "", memory_auto_reject: true, memory_write_mode: "review",
  memory_llm_judge: false, agent_adjudication: false, allow_self_review: false,
  credential_id: null, model_override: "",
};

const edge = (src: string, dst: string, over = {}) => ({
  id: `pe_${src}_${dst}`, src, dst, kind: "depends_on" as const, resolved_name: "@acme/core",
  evidence: [{ file: "web/package.json", fact: "^2.1" }], weight: 1, fresh: true,
  reason: "", updated_at: null, ...over,
});

const galaxy = (edges: unknown[] = []) => ({
  nodes: [
    { id: "prj_core", tag: "CORE", name: "Core", accent: "#c6f24e", provides: [],
      node_count: 40, pushed: true },
    { id: "prj_web", tag: "WEB", name: "Web", accent: "#7ca2ff", provides: [],
      node_count: 12, pushed: true },
  ],
  edges,
  collisions: [],
});

const codeMap = (node_count = 40) => ({ nodes: [], edges: [], node_count, edge_count: 0, outbound: [] });

const fleet = {
  agents: [], online: 0, total: 0, by_role: {}, posture: "single-agent", roles: [],
  presence_ttl_seconds: 90, heartbeat_interval_seconds: 30, review_queue: [],
  clusters: [], seats: [], waves: [],
};

/** Non-zero on purpose. An all-zero payload is the page-wide empty state, which hides the grid. */
const dash = {
  items_total: 4,
  items_by_status: { backlog: 1, next: 1, in_progress: 1, review: 1, done: 0, blocked: 0 },
  effort_total: 0, done_count: 0, in_progress_count: 1, blocked_count: 0,
  requests_total: 1,
  requests_by_type: { bug: 1, feature: 0, enhancement: 0, feedback: 0 },
  requests_by_status: { new: 1 },
  shard_count: 2, prd_count: 1, mcp_calls: 3,
  recent_items: [{ id: "CORE-1", title: "Ship the home", status: "in_progress" as const, date: "Oct 22" }],
};

const counts = {
  items: 4, items_in_progress: 1, requests: 1, review: 2, triage_new: 1, lessons_published: 3,
};

const zeroDash = {
  ...dash,
  items_total: 0,
  items_by_status: { backlog: 0, next: 0, in_progress: 0, review: 0, done: 0, blocked: 0 },
  in_progress_count: 0, blocked_count: 0, requests_total: 0, requests_by_type: {},
  requests_by_status: {}, shard_count: 0, prd_count: 0, mcp_calls: 0, recent_items: [],
};

const zeroCounts = {
  items: 0, items_in_progress: 0, requests: 0, review: 0, triage_new: 0, lessons_published: 0,
};

vi.mock("@/lib/api", () => ({
  api: {
    projects: vi.fn(async () => [core]),
    orgs: vi.fn(async () => [{ id: "org_1", name: "Acme", plan: "team", role: "owner" }]),
    items: vi.fn(async () => [{ id: "CORE-1", status: "in_progress" }]),
    shards: vi.fn(async () => []),
    dashboard: vi.fn(async () => dash),
    counts: vi.fn(async () => counts),
    codeMap: vi.fn(async () => ({ nodes: [], edges: [], node_count: 40, edge_count: 0, outbound: [] })),
    prds: vi.fn(async () => []),
    fleet: vi.fn(async () => ({
      agents: [], online: 0, total: 0, by_role: {}, posture: "single-agent", roles: [],
      presence_ttl_seconds: 90, heartbeat_interval_seconds: 30, review_queue: [],
      clusters: [], seats: [], waves: [],
    })),
    orgGalaxy: vi.fn(async () => galaxy()),
  },
}));

function renderHome() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return Object.assign(
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/p/CORE"]}>
          <ProjectProvider>
            <Routes>
              <Route path="/p/:tag" element={<ProjectHome />} />
            </Routes>
          </ProjectProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
    { qc },
  );
}

const STALE_BANNER = /stale — last good counts/i;

describe("Project home", () => {
  beforeEach(async () => {
    const { api } = await import("@/lib/api");
    // Every read starts healthy. A case below that fails one says so, and the next case
    // does not inherit it — a leaked rejection turns "the empty state is absent" into a
    // test of nothing at all.
    vi.mocked(api.projects).mockResolvedValue([core] as never);
    vi.mocked(api.orgs).mockResolvedValue(
      [{ id: "org_1", name: "Acme", plan: "team", role: "owner" }] as never,
    );
    vi.mocked(api.items).mockResolvedValue([{ id: "CORE-1", status: "in_progress" }] as never);
    vi.mocked(api.shards).mockResolvedValue([] as never);
    vi.mocked(api.dashboard).mockResolvedValue(dash as never);
    vi.mocked(api.counts).mockResolvedValue(counts as never);
    vi.mocked(api.codeMap).mockResolvedValue(codeMap() as never);
    vi.mocked(api.prds).mockResolvedValue([] as never);
    vi.mocked(api.fleet).mockResolvedValue(fleet as never);
    vi.mocked(api.orgGalaxy).mockResolvedValue(galaxy() as never);
  });

  it("routes into the surfaces that already exist", async () => {
    // D7 adds no capability — it gives the existing app a place in the hierarchy.
    renderHome();
    expect(await screen.findByRole("link", { name: /Tracker/ }))
      .toHaveAttribute("href", "/p/CORE/tracker");
    expect(screen.getByRole("link", { name: /PRDs/ })).toHaveAttribute("href", "/p/CORE/prds");
    expect(screen.getByRole("link", { name: /Triage/ })).toHaveAttribute("href", "/p/CORE/triage");
  });

  it("shows what depends on this project, not only what it depends on", async () => {
    // The half you cannot see from inside the repo, and the half that decides whether a
    // change here is safe.
    const { api } = await import("@/lib/api");
    vi.mocked(api.orgGalaxy).mockResolvedValue(
      galaxy([edge("prj_web", "prj_core")]) as never,
    );
    renderHome();

    const dependedBy = (await screen.findByText("DEPENDED ON BY")).parentElement!;
    expect(within(dependedBy).getByText("WEB")).toBeInTheDocument();
    expect(within(dependedBy).getByText(/1 file/)).toBeInTheDocument();
    // The design dropped this link. The 2026-10-22 decision keeps it.
    expect(screen.getByRole("link", { name: "Open in galaxy →" }))
      .toHaveAttribute("href", consoleHref(orgPath("galaxy")));
  });

  it("says nothing depends on it, rather than leaving the section blank", async () => {
    renderHome();
    expect(await screen.findByText(/No sibling repo declares this one/)).toBeInTheDocument();
    expect(screen.getByText(/every dependency resolved to an external package/i))
      .toBeInTheDocument();
  });

  it("marks a stale dependency instead of dropping it", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.orgGalaxy).mockResolvedValue(
      galaxy([edge("prj_core", "prj_web", { fresh: false })]) as never,
    );
    renderHome();
    expect(await screen.findByText("stale")).toBeInTheDocument();
  });

  it("names an empty code graph as nothing described, not nothing to describe", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.codeMap).mockResolvedValue(codeMap(0) as never);
    renderHome();
    expect(await screen.findByText(/No deployment has pushed a code graph/)).toBeInTheDocument();
    expect(screen.getByText(/not because/)).toBeInTheDocument();
    // Answered zero is the amber "not pushed", and the stat cell may say 0. An unread
    // map must not — that case is the failure test below.
    const code = screen.getByRole("link", { name: /Code graph/ });
    expect(within(code).getByText("not pushed")).toHaveClass("text-st-review");
    expect(screen.getByText("Graph nodes").parentElement).toHaveTextContent("0");
  });

  // ── PRD-47 S17: the states this screen was missing ─────────────────────────────────────
  //
  // Each case asserts BOTH halves, because only the second one catches the defect class:
  // the affordance for what actually happened, and the absence of the reassuring claim
  // that used to stand in for it.

  it("jumps to the eight surfaces with live meta, on project paths", async () => {
    renderHome();

    // The region is on the page during the skeleton too, with no metas yet.
    await screen.findByText("1 in flight");
    const jump = screen.getByRole("region", { name: "Jump to" });
    const link = (name: RegExp) => within(jump).getByRole("link", { name });

    expect(link(/Tracker/)).toHaveAttribute("href", "/p/CORE/tracker");
    expect(link(/Tracker/)).toHaveTextContent("1 in flight");
    expect(link(/Triage/)).toHaveAttribute("href", "/p/CORE/triage");
    expect(link(/Triage/)).toHaveTextContent("1 new");
    expect(link(/PRDs/)).toHaveAttribute("href", "/p/CORE/prds");
    expect(link(/PRDs/)).toHaveTextContent("1 docs");
    expect(link(/Code graph/)).toHaveAttribute("href", "/p/CORE/code");
    expect(link(/Code graph/)).toHaveTextContent("40 nodes");
    expect(link(/^Memory/)).toHaveAttribute("href", "/p/CORE/memory-review");
    expect(link(/^Memory/)).toHaveTextContent("2 to review");
    expect(link(/Lessons/)).toHaveAttribute("href", "/p/CORE/lessons");
    expect(link(/Lessons/)).toHaveTextContent("3 published");
    expect(link(/Fleet/)).toHaveAttribute("href", "/p/CORE/fleet");
    expect(link(/Fleet/)).toHaveTextContent("0 online");
    expect(link(/^Live/)).toHaveAttribute("href", "/p/CORE/live");
    expect(link(/^Live/)).toHaveTextContent("0 on now");
  });

  it("placeholds the counts while a read is in flight rather than rendering 0", async () => {
    const { api } = await import("@/lib/api");
    // Never settles. The map answers 40, which is the point: an outstanding read used
    // to sit beside a confident number as though the strip were already measured.
    vi.mocked(api.dashboard).mockReturnValue(new Promise(() => {}) as never);
    renderHome();

    expect(await screen.findByLabelText("Loading counts")).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
    // Not a zero, and not the 40 graph nodes that DID arrive either — the strip is one
    // measurement, so it is withheld whole rather than half-guessed.
    expect(screen.queryByText("40")).not.toBeInTheDocument();
    expect(screen.queryByText("not pushed")).not.toBeInTheDocument();
    expect(screen.queryByText(/No deployment has pushed a code graph/)).not.toBeInTheDocument();
    // The tiles are static links, so navigation is not held hostage to the numbers.
    expect(screen.getByRole("link", { name: /Tracker/ })).toBeInTheDocument();
    // Metas stay off the tiles until every count has answered.
    expect(screen.getByRole("link", { name: /Tracker/ })).not.toHaveTextContent("in flight");
  });

  it("offers a retry when a count could not be read, and withholds the no-code-graph claim", async () => {
    const user = userEvent.setup();
    const { api } = await import("@/lib/api");
    vi.mocked(api.codeMap).mockRejectedValue(new Error("boom") as never);
    renderHome();

    expect(await screen.findByText(/the request failed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    // A count that was not read is not a zero, and the page says which one it is.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(1);
    // The graph cell itself is the unread one. Legend zeros elsewhere are real answers
    // from the dashboard, so this assertion stays on the cell and the tile.
    const graph = screen.getByText("Graph nodes").parentElement!;
    expect(graph).toHaveTextContent("—");
    expect(graph).not.toHaveTextContent("0");
    const code = screen.getByRole("link", { name: /Code graph/ });
    expect(code).toHaveTextContent("—");
    expect(code).not.toHaveTextContent("not pushed");

    // The other half: the strongest claim on the page stays unmade, and the failure does
    // not masquerade as the loading state either.
    expect(screen.queryByText(/No deployment has pushed a code graph/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Loading counts")).not.toBeInTheDocument();
    // First load never had a good read, so the stale banner would be a claim that it did.
    expect(screen.queryByText(STALE_BANNER)).not.toBeInTheDocument();

    // The retry re-issues the read that failed — a button that does nothing is the same
    // defect wearing an affordance.
    vi.mocked(api.codeMap).mockResolvedValue(codeMap(40) as never);
    await user.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() =>
      expect(screen.queryByText(/the request failed/i)).not.toBeInTheDocument(),
    );
    expect(await screen.findByText("40")).toBeInTheDocument();
  });

  it("names a failed dependency read instead of showing a panel with nothing in it", async () => {
    const user = userEvent.setup();
    const { api } = await import("@/lib/api");
    vi.mocked(api.orgGalaxy).mockRejectedValue(new Error("boom") as never);
    renderHome();

    // The section still appears — that is the difference between "not read" and "absent".
    expect(
      await screen.findByRole("heading", { name: "Cross-project dependencies" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/what depends on this project was not read/i)).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: /retry/i });

    // Both honest empties are claims that nothing depends on this project. A read that
    // failed may not make either of them, and this is the assertion that used to pass
    // against `return null`.
    expect(screen.queryByText(/No sibling repo declares this one/)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/every dependency resolved to an external package/i),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("DEPENDS ON")).not.toBeInTheDocument();
    expect(screen.queryByText("DEPENDED ON BY")).not.toBeInTheDocument();

    // Retry brings the real answer back.
    vi.mocked(api.orgGalaxy).mockResolvedValue(galaxy([edge("prj_web", "prj_core")]) as never);
    await user.click(retry);
    const dependedBy = (await screen.findByText("DEPENDED ON BY")).parentElement!;
    expect(within(dependedBy).getByText("WEB")).toBeInTheDocument();
  });

  it("omits the dependency panel while the galaxy has not answered", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.orgGalaxy).mockReturnValue(new Promise(() => {}) as never);
    renderHome();

    await screen.findByText("Items");
    // Unknown is a third answer: no heading, no honest empty, no failure copy.
    expect(screen.queryByRole("heading", { name: "Cross-project dependencies" })).not.toBeInTheDocument();
    expect(screen.queryByText(/No sibling repo declares this one/)).not.toBeInTheDocument();
    expect(screen.queryByText(/every dependency resolved to an external package/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/what depends on this project was not read/i)).not.toBeInTheDocument();
  });

  it("says nothing to summarise when every read came back zero", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.dashboard).mockResolvedValue(zeroDash as never);
    vi.mocked(api.counts).mockResolvedValue(zeroCounts as never);
    vi.mocked(api.codeMap).mockResolvedValue(codeMap(0) as never);
    vi.mocked(api.fleet).mockResolvedValue(fleet as never);
    renderHome();

    expect(await screen.findByText("Nothing to summarise yet")).toBeInTheDocument();
    // A strip of zeros, the jump tiles, and the graph warning are all measurements.
    expect(screen.queryByText("Memory shards")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Jump to" })).not.toBeInTheDocument();
    expect(screen.queryByText(/No deployment has pushed a code graph/)).not.toBeInTheDocument();
    expect(screen.queryByText("not pushed")).not.toBeInTheDocument();
    // The agent card is guidance, and it stays on an empty project.
    expect(screen.getByText("Put agents on the backlog")).toBeInTheDocument();
  });

  it("says nothing to summarise when the only non-zero figure is deployment-wide MCP calls", async () => {
    const { api } = await import("@/lib/api");
    // The seed that hid this state had thousands of calls. Any positive count is enough:
    // mcp_calls is the deployment's total, not a count of this project.
    vi.mocked(api.dashboard).mockResolvedValue({ ...zeroDash, mcp_calls: 15850 } as never);
    vi.mocked(api.counts).mockResolvedValue(zeroCounts as never);
    vi.mocked(api.codeMap).mockResolvedValue(codeMap(0) as never);
    vi.mocked(api.fleet).mockResolvedValue(fleet as never);
    renderHome();

    expect(await screen.findByText("Nothing to summarise yet")).toBeInTheDocument();
    expect(screen.queryByText("Memory shards")).not.toBeInTheDocument();
    expect(screen.queryByText("No items yet.")).not.toBeInTheDocument();
  });

  it("uses the per-panel empty lines when only part of the project is empty", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.dashboard).mockResolvedValue({ ...zeroDash, shard_count: 2 } as never);
    vi.mocked(api.counts).mockResolvedValue(zeroCounts as never);
    vi.mocked(api.codeMap).mockResolvedValue(codeMap(40) as never);
    vi.mocked(api.fleet).mockResolvedValue(fleet as never);
    renderHome();

    expect(await screen.findByText("No items yet.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create the first one in Tracker →" }))
      .toHaveAttribute("href", "/p/CORE/tracker");
    expect(screen.getByText("No requests yet.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add the feedback widget →" }))
      .toHaveAttribute("href", "/p/CORE/feedback-kit");
    expect(screen.getByText("Nothing has happened here yet.")).toBeInTheDocument();
    expect(screen.queryByText("Nothing to summarise yet")).not.toBeInTheDocument();
    // The stat grid stays: shards were measured, so a zero items cell is a real zero.
    expect(screen.getByText("Items").parentElement).toHaveTextContent("0");
    expect(screen.getByText("Memory shards").parentElement).toHaveTextContent("2");
  });

  it("does not call a healthy read stale", async () => {
    renderHome();
    expect(await screen.findByText("Items")).toBeInTheDocument();
    expect(screen.getByText("Items").parentElement).toHaveTextContent("4");
    expect(screen.queryByText(STALE_BANNER)).not.toBeInTheDocument();
    expect(screen.queryByText(/the request failed/i)).not.toBeInTheDocument();
  });

  it("keeps the last good counts and names them stale when a refresh fails", async () => {
    const { api } = await import("@/lib/api");
    const { qc } = renderHome();
    expect(await screen.findByText("Items")).toBeInTheDocument();
    expect(screen.getByText("Items").parentElement).toHaveTextContent("4");
    expect(screen.queryByText(STALE_BANNER)).not.toBeInTheDocument();

    vi.mocked(api.dashboard).mockRejectedValue(new Error("boom"));
    await qc.refetchQueries({ queryKey: ["dashboard", core.id] }).catch(() => undefined);

    expect(await screen.findByText(STALE_BANNER)).toBeInTheDocument();
    // The failed refresh must not replace the last read with — or with the first-load error.
    expect(screen.getByText("Items").parentElement).toHaveTextContent("4");
    expect(screen.queryByText(/A count showing — was not read/i)).not.toBeInTheDocument();
  });
});
