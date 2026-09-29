import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { ProjectHome } from "@/features/projecthome/ProjectHome";
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

vi.mock("@/lib/api", () => ({
  api: {
    projects: vi.fn(async () => [core]),
    orgs: vi.fn(async () => [{ id: "org_1", name: "Acme", plan: "team", role: "owner" }]),
    items: vi.fn(async () => [{ id: "CORE-1", status: "in_progress" }]),
    shards: vi.fn(async () => []),
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
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/p/CORE"]}>
        <ProjectProvider>
          <Routes>
            <Route path="/p/:tag" element={<ProjectHome />} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

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
  });

  // ── PRD-47 S17: the states this screen was missing ─────────────────────────────────────
  //
  // Each case asserts BOTH halves, because only the second one catches the defect class:
  // the affordance for what actually happened, and the absence of the reassuring claim
  // that used to stand in for it.

  it("splits the surfaces into Work and Agents-and-memory instead of one flat grid", async () => {
    renderHome();

    const work = (await screen.findByRole("heading", { name: "Work" })).closest("section")!;
    const agents = screen
      .getByRole("heading", { name: "Agents and memory" })
      .closest("section")!;

    // A group is what it contains AND what it excludes — eight cards in one grid satisfies
    // the first half for both groups and the second for neither.
    expect(within(work).getByRole("link", { name: /Tracker/ }))
      .toHaveAttribute("href", "/p/CORE/tracker");
    expect(within(work).getByRole("link", { name: /PRDs/ })).toBeInTheDocument();
    expect(within(work).getByRole("link", { name: /Triage/ })).toBeInTheDocument();
    expect(within(work).getByRole("link", { name: /Code graph/ })).toBeInTheDocument();
    expect(within(work).queryByRole("link", { name: /^Memory/ })).not.toBeInTheDocument();
    expect(within(work).queryByRole("link", { name: /^Live/ })).not.toBeInTheDocument();

    expect(within(agents).getByRole("link", { name: /^Memory/ }))
      .toHaveAttribute("href", "/p/CORE/memory-review");
    expect(within(agents).getByRole("link", { name: /Lessons/ })).toBeInTheDocument();
    expect(within(agents).getByRole("link", { name: /Fleet/ })).toBeInTheDocument();
    expect(within(agents).getByRole("link", { name: /^Live/ })).toBeInTheDocument();
    expect(within(agents).queryByRole("link", { name: /Tracker/ })).not.toBeInTheDocument();
    expect(within(agents).queryByRole("link", { name: /Code graph/ })).not.toBeInTheDocument();
  });

  it("placeholds the counts while a read is in flight rather than rendering 0", async () => {
    const { api } = await import("@/lib/api");
    // Never settles. The other five answer, which is the point: an outstanding read used
    // to sit beside four confident numbers as though it were one of them.
    vi.mocked(api.items).mockReturnValue(new Promise(() => {}) as never);
    renderHome();

    expect(await screen.findByLabelText("Loading counts")).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
    // Not a zero, and not the 40 graph nodes that DID arrive either — the strip is one
    // measurement, so it is withheld whole rather than half-guessed.
    expect(screen.queryByText("40")).not.toBeInTheDocument();
    // The cards are static links, so navigation is not held hostage to the numbers.
    expect(screen.getByRole("link", { name: /Tracker/ })).toBeInTheDocument();
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

    // The other half: the strongest claim on the page stays unmade, and the failure does
    // not masquerade as the loading state either.
    expect(screen.queryByText(/No deployment has pushed a code graph/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Loading counts")).not.toBeInTheDocument();

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
});
