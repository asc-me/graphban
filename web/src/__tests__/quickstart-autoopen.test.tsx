import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppFrame } from "@/components/shell/AppFrame";
import { api } from "@/lib/api";

/** GRPH-1451 — the first-run auto-open resumes at the first unfinished step, and a failed
 *  setup read still opens it (at ⓪) rather than never opening. */

vi.mock("@/features/ProjectContext", () => ({
  ProjectProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useProjectCtx: () => ({ projects: [{ id: "core" }], loading: false, active: { id: "core" }, notFound: false }),
}));
vi.mock("@/components/shell/ProjectBar", () => ({ ProjectBar: () => null }));
vi.mock("@/components/shell/AgentSidebar", () => ({ AgentSidebar: () => null }));
vi.mock("@/components/shell/CommandPalette", () => ({ CommandPalette: () => null }));
vi.mock("@/components/shell/LeftNav", () => ({ LeftNav: () => null, readSidebarCollapsed: () => false }));
vi.mock("@/components/shell/TopBar", () => ({ TopBar: () => null }));
vi.mock("@/features/docs/DocsReader", () => ({ DocsReader: () => null }));
vi.mock("@/features/tour/AppTour", () => ({ AppTour: () => null }));

const qsState: { current: Record<string, unknown> } = { current: {} };
vi.mock("@/features/onboarding/QuickStartDialog", () => ({
  QuickStartDialog: ({ open, initialStep }: { open: boolean; initialStep?: string }) =>
    open ? <div data-testid="qs-open" data-step={initialStep ?? "where"} /> : null,
  useQuickStart: () => qsState.current,
}));
vi.mock("@/lib/api", () => ({
  hasSession: () => true,
  api: {
    config: vi.fn(async () => ({ hosted_mode: false })),
    orgs: vi.fn(async () => []),
    onboarding: vi.fn(),
    markOnboarding: vi.fn(),
  },
}));

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AppFrame />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(api.onboarding).mockResolvedValue({ onboarding: null });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("GRPH-1451 quick start auto-open", () => {
  it("opens at the first unfinished step once the setup read answers", async () => {
    qsState.current = { setup: { steps: [] }, setupSettled: true, firstUnfinished: "model" };
    mount();
    expect((await screen.findByTestId("qs-open")).dataset.step).toBe("model");
  });

  it("still opens, at ⓪, when the setup read failed", async () => {
    qsState.current = { setup: undefined, setupSettled: true, firstUnfinished: "model" };
    mount();
    expect((await screen.findByTestId("qs-open")).dataset.step).toBe("where");
  });

  it("waits while the setup read is in flight", async () => {
    qsState.current = { setup: undefined, setupSettled: false, firstUnfinished: "where" };
    mount();
    await waitFor(() => expect(api.onboarding).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId("qs-open")).toBeNull();
  });
});
