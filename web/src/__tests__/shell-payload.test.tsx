/**
 * The shell asks for numbers, not collections (GRPH-431).
 *
 * Reported as "the PRD page isn't loading". It was not failing — every request returned 200.
 * One load of `/prds` pulled 765 KB of items, 740 KB of memory shards and 621 KB of candidates
 * to render three nav badges and one stat, while the data the page exists to show was 2.8 KB.
 *
 * This asserts the shape rather than the symptom, because the symptom is a stopwatch. If
 * somebody reaches for `useItems` in the nav again to get a `.length`, this fails immediately
 * and says why — where the real-world signal is a page that gradually stops feeling loaded, on
 * a project big enough that nobody can bisect it.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LeftNav } from "@/components/shell/LeftNav";
import { ProjectBar } from "@/components/shell/ProjectBar";
import { ProjectProvider } from "@/features/ProjectContext";

const project = {
  id: "core", tag: "CORE", name: "Core", accent: "#c6f24e", visibility: "private",
  description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
  embed_model: "",
};

// Every collection endpoint the shell used to call. Each returns a row so that a component
// still reading `.length` off one would render a plausible number and pass a weaker test —
// the assertion has to be that it was never ASKED, not that the answer was empty.
// `vi.hoisted` because `vi.mock` is hoisted above the file's own declarations.
const collections = vi.hoisted(() => ({
  items: vi.fn(async () => [{ id: "X-1", status: "in_progress" }]),
  shards: vi.fn(async () => [{ id: "m1" }]),
  candidateShards: vi.fn(async () => [{ id: "m2" }]),
  autoActions: vi.fn(async () => [{ id: "m3", scoring_source: "trusted" }]),
  requests: vi.fn(async () => [{ id: "R-1" }]),
}));

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [project]),
    counts: vi.fn(async () => ({ items: 41, items_in_progress: 3, requests: 7, review: 5 })),
    orgs: vi.fn(async () => []),
    adminWhoami: vi.fn(async () => ({ is_platform_admin: false })),
    syncStatus: vi.fn(async () => ({
      linked: false, source: "", cloud_url: "", org: "", credential_set: false, linked_at: null, projects: [],
    })),
    ...collections,
  },
}));

function renderShell(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/tracker"]}>
        <ProjectProvider>{ui}</ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("the app shell", () => {
  beforeEach(() => {
    for (const fn of Object.values(collections)) fn.mockClear();
  });

  it("renders nav badges from counts, never from collections", async () => {
    renderShell(<LeftNav />);
    // 41 items comes from `counts`, and the mocked `items` collection has length 1 — so a
    // component that regressed to `.length` would render 1 and fail here as well.
    expect(await screen.findByText("41")).toBeInTheDocument();

    for (const [name, fn] of Object.entries(collections)) {
      expect(fn, `the shell fetched the whole ${name} collection (GRPH-431)`).not.toHaveBeenCalled();
    }
  });

  it("renders the project bar's stats from counts, never from collections", async () => {
    renderShell(<ProjectBar />);
    expect(await screen.findByText("41")).toBeInTheDocument();

    for (const [name, fn] of Object.entries(collections)) {
      expect(fn, `the project bar fetched the whole ${name} collection (GRPH-431)`)
        .not.toHaveBeenCalled();
    }
  });
});
