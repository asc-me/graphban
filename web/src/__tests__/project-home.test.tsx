import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
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
    vi.mocked(api.orgGalaxy).mockResolvedValue(galaxy() as never);
    vi.mocked(api.codeMap).mockResolvedValue(
      { nodes: [], edges: [], node_count: 40, edge_count: 0, outbound: [] } as never,
    );
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
    vi.mocked(api.codeMap).mockResolvedValue(
      { nodes: [], edges: [], node_count: 0, edge_count: 0, outbound: [] } as never,
    );
    renderHome();
    expect(await screen.findByText(/No deployment has pushed a code graph/)).toBeInTheDocument();
    expect(screen.getByText(/not because/)).toBeInTheDocument();
  });
});
