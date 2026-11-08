import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppFrame } from "@/components/shell/AppFrame";
import { api } from "@/lib/api";

/** GRPH-1444 — a backfilled (finished) user is offered "Take the tour" once, recorded server-side. */

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
vi.mock("@/features/tour/AppTour", () => ({
  AppTour: ({ open }: { open: boolean }) => (open ? <div data-testid="tour-open" /> : null),
}));
vi.mock("@/features/onboarding/QuickStartDialog", () => ({
  QuickStartDialog: () => null,
  useQuickStart: () => ({}),
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
  vi.mocked(api.markOnboarding).mockResolvedValue({
    onboarding: { quickstart_finished_at: "t", tour_offered_at: "t" },
  });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("GRPH-1444 one-time tour toast", () => {
  it("shows once for a backfilled user and records tour_offered", async () => {
    vi.mocked(api.onboarding).mockResolvedValue({ onboarding: { quickstart_finished_at: "t" } });
    mount();
    await screen.findByTestId("tour-toast");
    await waitFor(() => expect(api.markOnboarding).toHaveBeenCalledTimes(1));
    expect(api.markOnboarding).toHaveBeenCalledWith("tour_offered");
    screen.getByRole("button", { name: "Take the tour" }).click();
    await screen.findByTestId("tour-open");
  });

  it("never shows again once tour_offered is stored", async () => {
    vi.mocked(api.onboarding).mockResolvedValue({
      onboarding: { quickstart_finished_at: "t", tour_offered_at: "t" },
    });
    mount();
    await waitFor(() => expect(api.onboarding).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId("tour-toast")).toBeNull();
    expect(api.markOnboarding).not.toHaveBeenCalled();
  });
});
