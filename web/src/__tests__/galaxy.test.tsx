import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GalaxyView } from "@/features/galaxy/GalaxyView";
import type { Galaxy, GalaxyEdge, GalaxyNode } from "@/lib/types";

/**
 * PRD-21 D3.
 *
 * The three empty states are the point of this screen: "no repos", "repos but nothing
 * pushed" and "pushed, nothing internal" are three different facts about an org, and
 * rendering them identically is the specific failure this product is built to avoid.
 */
const node = (tag: string, over: Partial<GalaxyNode> = {}): GalaxyNode => ({
  id: `prj_${tag.toLowerCase()}`, tag, name: tag, accent: "#c6f24e",
  provides: [], node_count: 40, pushed: true, ...over,
});

const edge = (over: Partial<GalaxyEdge> = {}): GalaxyEdge => ({
  id: "pe_1", src: "prj_web", dst: "prj_core", kind: "depends_on",
  resolved_name: "@acme/core",
  evidence: [{ file: "web/package.json", fact: "@acme/core ^2.1" }],
  weight: 1, fresh: true, reason: "", updated_at: null, ...over,
});

const galaxy = (over: Partial<Galaxy> = {}): Galaxy => ({
  nodes: [node("WEB"), node("CORE")], edges: [edge()], collisions: [], ...over,
});

vi.mock("@/lib/api", () => ({
  api: {
    orgs: vi.fn(async () => [{ id: "org_1", name: "Acme", plan: "team", role: "owner" }]),
    orgGalaxy: vi.fn(async () => galaxy()),
  },
}));

// The layout worker is not available in jsdom; positions are the view's input, not its
// contract, so it is stubbed to a deterministic grid.
vi.mock("@/lib/graph/useGraphLayout", () => ({
  useGraphLayout: (ids: string[]) => ({
    pos: Object.fromEntries(ids.map((id, i) => [id, { x: 100 + i * 200, y: 300 }])),
    pending: false,
    relayout: vi.fn(),
  }),
}));

function renderGalaxy() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/org/galaxy"]}>
        <GalaxyView />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Galaxy", () => {
  // `mockResolvedValueOnce` leaks between cases here: a previous test's unmounted render
  // can still consume a queued value. Reset to a known default and override per case.
  beforeEach(async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.orgGalaxy).mockResolvedValue(galaxy());
  });

  it("does not claim the org is empty before it has an answer", async () => {
    // `isLoading` is false while the org id resolves, so an unguarded view renders "No
    // projects yet" for a frame — a confident statement made from not having looked.
    renderGalaxy();
    expect(screen.queryByText("No projects yet")).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Galaxy" })).toBeInTheDocument();
  });

  it("shows the file that proves an edge when you hover it", async () => {
    // The load-bearing interaction. Every edge in this product can name its proof, and
    // making that visible is the difference between this graph and a guess.
    renderGalaxy();
    fireEvent.pointerEnter(await screen.findByTestId("edge-pe_1"));
    expect(screen.getByText("web/package.json")).toBeInTheDocument();
    expect(screen.getByText(/@acme\/core \^2\.1/)).toBeInTheDocument();
  });

  it("distinguishes an org with no repos from one whose edges are empty", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.orgGalaxy).mockResolvedValue(galaxy({ nodes: [], edges: [] }));
    const first = renderGalaxy();
    expect(await screen.findByText("No projects yet")).toBeInTheDocument();
    first.unmount();

    // Repos exist but nothing has pushed: the NODES are drawn, because the org is not
    // empty — its edges are.
    vi.mocked(api.orgGalaxy).mockResolvedValue(
      galaxy({ nodes: [node("WEB", { pushed: false, node_count: 0 })], edges: [] }),
    );
    renderGalaxy();
    expect(await screen.findByText(/No deployment has pushed a manifest yet/))
      .toBeInTheDocument();
    expect(screen.getByText("WEB")).toBeInTheDocument();
  });

  it("treats no internal dependencies as an answer, not a failure", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.orgGalaxy).mockResolvedValue(
      galaxy({ nodes: [node("WEB"), node("CORE")], edges: [] }),
    );
    renderGalaxy();
    expect(await screen.findByText(/Every dependency resolved to an external package/))
      .toBeInTheDocument();
    expect(screen.getByText(/not a failure to compute one/)).toBeInTheDocument();
  });

  it("hides stale edges behind a toggle and says how many", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.orgGalaxy).mockResolvedValue(
      galaxy({ edges: [edge({ fresh: false })] }),
    );
    renderGalaxy();
    expect(await screen.findByText(/1 edge hidden/)).toBeInTheDocument();
    expect(screen.getByText(/kept rather than deleted/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /show stale/i }));
    fireEvent.pointerEnter(screen.getByTestId("edge-pe_1"));
    // Its evidence survives — a relationship with no explanation is worse than none.
    expect(screen.getByText("web/package.json")).toBeInTheDocument();
    expect(screen.getByText(/Kept, not deleted/)).toBeInTheDocument();
  });

  it("does not call a hidden stale edge an absence of dependencies", async () => {
    // Filtering stale edges out of the VIEW does not mean the org has none. Keying the
    // empty state on the filtered list made the user's own toggle produce a confident
    // wrong answer — "every dependency resolved to an external package" — about an org
    // whose one internal dependency had simply gone stale.
    const { api } = await import("@/lib/api");
    vi.mocked(api.orgGalaxy).mockResolvedValue(galaxy({ edges: [edge({ fresh: false })] }));
    renderGalaxy();

    expect(await screen.findByText(/Every dependency here has gone stale/)).toBeInTheDocument();
    expect(screen.queryByText(/resolved to an external package/)).not.toBeInTheDocument();
  });

  it("says a contested name drew nothing, rather than drawing a guess", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.orgGalaxy).mockResolvedValue(
      galaxy({ collisions: [{ name: "@acme/shared", project_ids: ["prj_web", "prj_core"] }] }),
    );
    renderGalaxy();
    expect(await screen.findByText("@acme/shared")).toBeInTheDocument();
    expect(screen.getByText(/no edge is drawn/i)).toBeInTheDocument();
    expect(screen.getByText(/does not guess/)).toBeInTheDocument();
  });
});
