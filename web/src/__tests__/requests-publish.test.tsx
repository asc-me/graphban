import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
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

const { publishSpy, unpublishSpy, commentSpy } = vi.hoisted(() => ({
  publishSpy: vi.fn(async () => ({ published: true, published_at: "2026-01-01T00:00:00Z" })),
  unpublishSpy: vi.fn(async () => ({ published: false })),
  commentSpy: vi.fn(async () => ({ id: "c1", body: "test", visibility: "private", created_at: "2026-01-01T00:00:00Z" })),
}));

vi.mock("@/lib/api", () => ({
  api: {
    projects: vi.fn(async () => [core]),
    requests: vi.fn(async () => [makeReq()]),
    voteRequest: vi.fn(async () => ({})),
    publishRequest: publishSpy,
    unpublishRequest: unpublishSpy,
    createRequestComment: commentSpy,
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
