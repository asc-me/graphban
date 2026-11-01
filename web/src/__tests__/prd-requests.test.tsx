import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { PrdListView } from "@/features/prds/PrdListView";
import { PrdRequestsPanel } from "@/features/prds/PrdRequests";

/** Linked requests on PRDs (GRPH-1150): list dots, the Requests panel, suggestions. */

const mocks = vi.hoisted(() => ({
  prds: vi.fn(),
  prdRequests: vi.fn(),
  prdRequestSuggestions: vi.fn(),
  linkPrdRequest: vi.fn(),
  unlinkPrdRequest: vi.fn(),
  requests: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [{ id: "core", name: "Core", tag: "GRPH" }]),
    me: vi.fn(async () => ({ name: "Alex Cain" })),
    ...mocks,
  },
}));

const req = (id: string, type: string, votes: number) => ({ id, title: `Req ${id}`, type, votes, status: "new", by: "", created_at: null, linked_to: null });

function wrap(ui: React.ReactNode, path = "/prds") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>{ui}</ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  Object.values(mocks).forEach((m) => m.mockReset());
  mocks.requests.mockResolvedValue([]);
  localStorage.removeItem("gb_views_prds");
});

function Where() {
  const l = useLocation();
  return <div data-testid="where">{l.pathname + l.search}</div>;
}

describe("PRD list request pill", () => {
  it("shows type dots with counts, a tooltip, and opens the Requests tab", async () => {
    mocks.prds.mockResolvedValue([
      { id: "GRPH-P1", title: "With", status: "draft", version: "v1", linked: [], updated_at: new Date().toISOString(),
        request_count: 3, request_votes: 12, request_types: { bug: 2, feature: 1 } },
      { id: "GRPH-P2", title: "Without", status: "draft", version: "v1", linked: [], updated_at: new Date().toISOString(),
        request_count: 0, request_votes: 0, request_types: {} },
    ]);
    wrap(<><Where /><Routes><Route path="/prds" element={<PrdListView />} /><Route path="/prds/:id" element={null} /></Routes></>);
    const pills = await screen.findAllByTestId("prd-request-dots");
    expect(pills[0]).toHaveAttribute("title", "3 linked requests · 12 votes — open");
    expect(pills[0]).toHaveTextContent("2");
    expect(pills[0]).toHaveTextContent("1");
    expect(pills[1]).toHaveTextContent("no requests");
    await userEvent.setup().click(pills[0]);
    expect(screen.getByTestId("where")).toHaveTextContent("/prds/GRPH-P1?tab=requests");
  });
});

describe("Requests panel", () => {
  it("sorts by votes, filters by type, and unlinks", async () => {
    mocks.prdRequests.mockResolvedValue([req("GRPH-R1", "bug", 2), req("GRPH-R2", "feature", 9), req("GRPH-R3", "bug", 5)]);
    mocks.prdRequestSuggestions.mockResolvedValue({ available: true, suggestions: [] });
    mocks.unlinkPrdRequest.mockResolvedValue({});
    wrap(<PrdRequestsPanel prdId="GRPH-P1" projectId="core" />);
    const panel = await screen.findByTestId("prd-requests-panel");
    expect(await within(panel).findByText("3 · 16 votes")).toBeInTheDocument();
    const order = () => within(panel).getAllByText(/^Req GRPH-R\d$/).map((e) => e.textContent);
    expect(order()).toEqual(["Req GRPH-R2", "Req GRPH-R3", "Req GRPH-R1"]);
    const user = userEvent.setup();
    await user.click(within(panel).getByRole("button", { name: /^Bug/ }));
    expect(order()).toEqual(["Req GRPH-R3", "Req GRPH-R1"]);
    await user.click(within(panel).getAllByText("Unlink")[0]);
    await waitFor(() => expect(mocks.unlinkPrdRequest).toHaveBeenCalledWith("GRPH-P1", "GRPH-R3"));
  });

  it("shows submitter, relative time and linked item; omits by when empty", async () => {
    const created_at = new Date(Date.now() - 3 * 3_600_000).toISOString();
    mocks.prdRequests.mockResolvedValue([
      { ...req("GRPH-R1", "bug", 2), by: "dana", created_at, linked_to: "GRPH-42" },
      { ...req("GRPH-R2", "bug", 1), created_at },
    ]);
    mocks.prdRequestSuggestions.mockResolvedValue({ available: true, suggestions: [] });
    wrap(<PrdRequestsPanel prdId="GRPH-P1" projectId="core" />);
    expect(await screen.findByText("dana · 3h ago")).toBeInTheDocument();
    expect(screen.getByText("→ GRPH-42")).toBeInTheDocument();
    expect(screen.getByText("3h ago")).toBeInTheDocument();
  });

  it("links from a suggestion and the list refetches", async () => {
    let linked = [req("GRPH-R1", "bug", 1)];
    mocks.prdRequests.mockImplementation(async () => linked);
    mocks.prdRequestSuggestions.mockResolvedValue({
      available: true, suggestions: [{ request_id: "GRPH-R7", title: "Dupe", type: "feature", votes: 4, score: 0.87 }],
    });
    mocks.linkPrdRequest.mockImplementation(async () => { linked = [...linked, req("GRPH-R7", "feature", 4)]; return {}; });
    wrap(<PrdRequestsPanel prdId="GRPH-P1" projectId="core" />);
    expect(await screen.findByText("87% match")).toBeInTheDocument();
    expect(screen.getByText("SUGGESTED · DUPLICATE DETECTION")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Link" }));
    expect(await screen.findByText("2 · 5 votes")).toBeInTheDocument();
  });

  it("says suggestions are unavailable rather than rendering an empty section", async () => {
    mocks.prdRequests.mockResolvedValue([]);
    mocks.prdRequestSuggestions.mockResolvedValue({ available: false, suggestions: [] });
    wrap(<PrdRequestsPanel prdId="GRPH-P1" projectId="core" />);
    expect(await screen.findByText("Suggestions unavailable")).toBeInTheDocument();
    expect(screen.queryByText("SUGGESTED · DUPLICATE DETECTION")).not.toBeInTheDocument();
    expect(screen.getByText(/No bugs or feature requests are linked to this PRD yet/)).toBeInTheDocument();
  });
});
