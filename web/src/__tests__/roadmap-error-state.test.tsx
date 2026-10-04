/**
 * GRPH-943 — Roadmap's three states must stay three states.
 *
 * `absence-vs-failure.test.tsx` holds the failure half (its mock rejects every endpoint,
 * so it cannot express the others). This file pins what that mock cannot:
 *
 *   1. a genuinely empty board still says "No milestones in this project" — the `isError`
 *      branch must not swallow the empty one, which is the other way this fix could go
 *      wrong, and the reason GRPH-924's copy still means what it says;
 *   2. a loaded board renders the board;
 *   3. Retry actually re-issues the read. Asserting only that the button *renders* leaves
 *      `onRetry` free to be a no-op — the defect would then be a failure with an exit that
 *      goes nowhere, wearing the same clothes as the fixed one.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { RoadmapView } from "@/features/roadmap/RoadmapView";
import type { RoadmapPhase } from "@/lib/types";

const project = {
  id: "core", name: "Core", tag: "GRPH", accent: "#c6f24e", description: "",
};

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [project]),
    config: vi.fn(async () => ({ hosted_mode: false })),
    roadmap: vi.fn(async (): Promise<RoadmapPhase[]> => []),
  },
}));

const phase: RoadmapPhase = {
  key: "mvp",
  name: "MVP",
  window: "2026 Q3",
  color: "#c6f24e",
  done: 1,
  total: 2,
  milestones: [
    { title: "Ledger loop", tag: "GRPH-1", done: true },
    { title: "Public roadmap", tag: "GRPH-2", done: false },
  ],
};

function show() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ProjectProvider>
          <RoadmapView />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function roadmapMock() {
  const { api } = await import("@/lib/api");
  return vi.mocked(api.roadmap);
}

beforeEach(async () => {
  vi.clearAllMocks();
  (await roadmapMock()).mockResolvedValue([]);
});

describe("Roadmap — empty is not failure", () => {
  it("a board with no milestones still says so, and does not claim a failure", async () => {
    show();
    expect(await screen.findByText("No milestones in this project")).toBeInTheDocument();
    expect(screen.getByText("PRDs still live under PRDs.")).toBeInTheDocument();
    expect(screen.queryByText(/roadmap unavailable/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
  });

  it("a loaded board renders its phases, not the error or the empty copy", async () => {
    (await roadmapMock()).mockResolvedValue([phase]);
    show();
    expect(await screen.findByText("MVP")).toBeInTheDocument();
    expect(screen.getByText("Ledger loop")).toBeInTheDocument();
    expect(screen.getByText("1 / 2 shipped")).toBeInTheDocument();
    expect(screen.queryByText(/roadmap unavailable/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/no milestones in this project/i)).not.toBeInTheDocument();
  });
});

describe("Roadmap — failure is not empty", () => {
  it("Retry re-issues the roadmap read", async () => {
    const roadmap = await roadmapMock();
    roadmap.mockRejectedValue(new Error("boom"));
    show();

    expect(await screen.findByText(/roadmap unavailable/i)).toBeInTheDocument();
    expect(screen.queryByText(/no milestones in this project/i)).not.toBeInTheDocument();

    const callsBefore = roadmap.mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(roadmap.mock.calls.length).toBeGreaterThan(callsBefore);
  });
});
