import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { RequestsView } from "@/features/requests/RequestsView";
import type { Project, RequestItem } from "@/lib/types";

const core: Project = {
  id: "prj_core", name: "Core", tag: "CORE", accent: "#c6f24e", visibility: "private",
  description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
  embed_model: "",
  credential_id: null,
  model_override: "", memory_auto_reject: true, memory_write_mode: "review",
  memory_llm_judge: false, agent_adjudication: false, allow_self_review: false,
};

const makeReq = (overrides: Partial<RequestItem> = {}): RequestItem => ({
  id: "CORE-R1", project_id: "prj_core", type: "bug", title: "Test request",
  detail: "Some detail", by: "dana", votes: 3, status: "new", linked_to: null,
  ago: "2h", source_url: "", meta: {}, attachment_ids: [], created_at: "",
  published_at: null, ...overrides,
});

const { publishSpy, unpublishSpy, commentSpy, voteSpy } = vi.hoisted(() => ({
  publishSpy: vi.fn(async () => ({ published: true, published_at: "2026-01-01T00:00:00Z" })),
  unpublishSpy: vi.fn(async () => ({ published: false })),
  commentSpy: vi.fn(async () => ({ id: "c1", body: "test", visibility: "private", created_at: "2026-01-01T00:00:00Z" })),
  voteSpy: vi.fn(async () => ({})),
}));

vi.mock("@/lib/api", () => ({
  api: {
    projects: vi.fn(async () => [core]),
    requests: vi.fn(async () => [makeReq()]),
    voteRequest: voteSpy,
    publishRequest: publishSpy,
    unpublishRequest: unpublishSpy,
    createRequestComment: commentSpy,
    // The expanded row mounts `LinkedCode`, which reads the request's code links. Served
    // as an empty list so the no-links copy is asserted against a real answer rather than
    // against a query that threw because nothing was mocked.
    codeForRef: vi.fn(async () => []),
    fleet: vi.fn(async () => ({ agents: [], online: 0, total: 0, by_role: {}, posture: "single-agent", roles: [], presence_ttl_seconds: 90, heartbeat_interval_seconds: 30, review_queue: [], clusters: [], seats: [], waves: [] })),
  },
}));

function renderRequests() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/p/CORE/requests"]}>
        <ProjectProvider>
          <RequestsView />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Requests publish/comment controls (GRPH-903 sabotage)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a Publish button for unpublished requests", async () => {
    renderRequests();
    const btn = await screen.findByTitle(/publish.*public board/i);
    expect(btn).toBeTruthy();
    expect(btn.textContent).toMatch(/publish/i);
  });

  it("shows a Published indicator for already-published requests", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.requests).mockResolvedValue([makeReq({ published_at: "2026-01-01T00:00:00Z" })] as never);
    renderRequests();
    const btn = await screen.findByTitle(/unpublish.*public board/i);
    expect(btn).toBeTruthy();
    expect(btn.textContent).toMatch(/published/i);
  });

  it("has a comment form with a public visibility toggle in the expanded row", async () => {
    renderRequests();
    const expandBtn = await screen.findByTitle(/show detail/i);
    expandBtn.click();
    const publicCheckbox = await screen.findByLabelText(/public/i);
    expect(publicCheckbox).toBeTruthy();
    const textarea = screen.getByPlaceholderText(/write a comment/i);
    expect(textarea).toBeTruthy();
  });
});

/**
 * The rest of the row PRD-47 §S3 names: the vote count, the type chip, the detail that
 * expands in place, and the linked-code list with its no-links copy.
 *
 * "Expand in place" is the part worth pinning. The detail has to be absent from the
 * document before the row is opened — a row that rendered it hidden and merely toggled a
 * class would still "show the detail on expand", and would also ship every request's
 * body to the DOM on first paint.
 */
describe("the request row (PRD-47 S3)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows the vote count and the type chip without expanding", async () => {
    renderRequests();
    const vote = await screen.findByTitle("Upvote");
    expect(vote).toHaveTextContent("3");

    // Scoped to the row: the filter bar above the list carries a "BUG" chip of its own,
    // and matching either one would not show that the row is typed.
    const row = vote.closest<HTMLDivElement>("div[class*='rounded-[12px]']")!;
    expect(within(row).getByText("BUG")).toBeInTheDocument();
  });

  it("counts a vote from the row's vote control", async () => {
    renderRequests();
    await userEvent.click(await screen.findByTitle("Upvote"));
    expect(voteSpy).toHaveBeenCalledWith("CORE-R1", 1);
  });

  it("expands in place to the detail and the linked-code list", async () => {
    renderRequests();
    // Wait for the row itself first. Asserting the detail is absent before the query
    // resolves passes against a list with no rows in it at all, which says nothing about
    // whether the row hides its detail — a sabotage that rendered the detail permanently
    // walked straight through that version of this test.
    await screen.findByText("Test request");
    expect(screen.queryByText("Some detail")).not.toBeInTheDocument();

    await userEvent.click(screen.getByTitle(/show detail/i));

    expect(await screen.findByText("Some detail")).toBeInTheDocument();
    expect(screen.getByText("No code linked yet.")).toBeInTheDocument();
    // Still the same row, still on the same page: the title is above the detail.
    expect(screen.getByText("Test request")).toBeInTheDocument();
  });
});
