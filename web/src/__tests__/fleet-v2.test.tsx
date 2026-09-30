import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FleetV2View } from "@/features/fleet/FleetV2View";

const api = vi.hoisted(() => ({
  saveFleetProfile: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api }));

vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ active: { id: "core", name: "Core", tag: "core" },
                          activeId: "core", projects: [] }),
}));

const fleet = vi.hoisted(() => ({ data: null as unknown, refetch: vi.fn() }));
const tierMap = vi.hoisted(() => ({
  data: null as { rows: unknown[]; cells: unknown[]; overridden: boolean } | null,
  isFetching: false,
  isError: false,
  refetch: vi.fn(),
}));
vi.mock("@/lib/queries", () => ({
  useFleet: () => fleet,
  useConfig: () => ({ data: { hosted_mode: false } }),
  // The tier map has its own query so a 15s poll cannot land under an unsaved edit; it is
  // stubbed here because this file is about the catalog and the mix. `fleet-tier-map.test.tsx`
  // drives the real hooks.
  useFleetTierMap: () => tierMap,
  useSaveFleetTierMap: () => ({ isPending: false, isError: false, error: null, mutate: vi.fn() }),
  useClearFleetTierMap: () => ({ isPending: false, isError: false, error: null, mutate: vi.fn() }),
}));

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <FleetV2View />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const BASE = {
  agents: [], online: 0, total: 0, roles: ["planner", "worker"],
  by_role: {}, posture: "single-agent",
  presence_ttl_seconds: 150, heartbeat_interval_seconds: 50,
  review_queue: [], clusters: [], seats: [], credentials: [], waves: ["wave-1"],
  profile: null, policy: null, measured: [],
  matrix: { rows: [] }, mix: { n: 0, by_harness: {}, unreported: 0 },
};

const rows = [
  { harness: "gbagent", model: "qwen3.6", vendor: "gbagent", lane: "any",
    tier: "cheap", status: "verified", cost_class: "local", local: true },
  { harness: "claude", model: "sonnet", vendor: "anthropic", lane: "any",
    tier: "cheap", status: "unverified", cost_class: "cheap", local: false },
  { harness: "codex", model: "", vendor: "openai", lane: "any",
    tier: "frontier", status: "unregistered", cost_class: "frontier", local: false },
];

describe("Fleet.v2 catalog and allocation", () => {
  beforeEach(() => {
    fleet.data = { ...BASE };
    tierMap.data = null;
    fleet.refetch.mockReset();
    api.saveFleetProfile.mockReset();
  });

  it("names itself Fleet.v2 and does not mix in the roster", () => {
    fleet.data = { ...BASE, matrix: { rows } };
    renderView();
    expect(screen.getByTestId("fleet-v2")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toMatch(/Fleet\.v2\s*core/);
    expect(screen.queryByRole("button", { name: /^Work/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Wave/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Fleet.v1" })).toHaveAttribute("href", "/fleet.v1");
  });

  it("draws unique matrix rows and links to Observe Harness", () => {
    fleet.data = { ...BASE, matrix: { rows } };
    renderView();
    expect(screen.getByTestId("fleet-matrix")).toHaveTextContent("gbagent");
    expect(screen.getByTestId("fleet-matrix")).toHaveTextContent("sonnet");
    expect(screen.getByTestId("fleet-matrix")).toHaveTextContent("unregistered");
    expect(screen.getByTestId("fleet-matrix-observe")).toHaveAttribute("href", "/harness");
  });

  it("saves mix percents and omits unregistered harnesses", async () => {
    fleet.data = { ...BASE, matrix: { rows } };
    api.saveFleetProfile.mockResolvedValue({ scope: "default" });
    const user = userEvent.setup();
    renderView();
    expect(screen.queryByLabelText("Share codex")).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("Allocate by share"));
    fireEvent.change(screen.getByLabelText("Share gbagent"), { target: { value: "40" } });
    fireEvent.change(screen.getByLabelText("Share claude"), { target: { value: "60" } });
    await user.click(screen.getByRole("button", { name: /Save allocation/ }));
    await waitFor(() => expect(api.saveFleetProfile).toHaveBeenCalledTimes(1));
    expect(api.saveFleetProfile.mock.calls[0][0].mix).toEqual({ gbagent: 0.4, claude: 0.6 });
  });

  it("saves null mix when allocation is off", async () => {
    fleet.data = {
      ...BASE,
      matrix: { rows },
      profile: { user: "u", project_id: null, scope: "default", defaults: [],
                 weights: {}, excludes: [], mix: { gbagent: 1 }, updated_at: null },
    };
    api.saveFleetProfile.mockResolvedValue({ scope: "default" });
    const user = userEvent.setup();
    renderView();
    await user.click(screen.getByLabelText("Allocate by share"));
    await user.click(screen.getByRole("button", { name: /Save allocation/ }));
    await waitFor(() => expect(api.saveFleetProfile).toHaveBeenCalledTimes(1));
    expect(api.saveFleetProfile.mock.calls[0][0].mix).toBeNull();
  });

  /**
   * The CALL, not just the callee (AGENTS.md). `fleet-tier-map.test.tsx` renders the panel
   * directly and can stay green against a panel nobody mounted; deleting `<TierMapPanel>`
   * from FleetV2View, or handing it the wrong project, has to fail HERE.
   */
  it("renders the editable tier map on the page, scoped to the active project", () => {
    fleet.data = { ...BASE, matrix: { rows } };
    tierMap.data = {
      rows: [],
      overridden: false,
      cells: [{
        harness: "gbagent", tier: "cheap", packaged_model: "qwen3.6", override: null,
        effective_model: "qwen3.6", overridden: false, models: ["qwen3.6", "haiku"],
        graded_model: "haiku",
      }],
    };
    renderView();
    const panel = screen.getByTestId("fleet-tier-map");
    expect(within(panel).getByRole("heading", { name: "Tier map" })).toBeInTheDocument();
    // The read-only catalog is still there beside it — the panel is an addition, not a swap.
    expect(screen.getByTestId("fleet-matrix")).toHaveTextContent("gbagent");
    expect(within(panel).getByRole("button", { name: /Inherit from performance grading/ }))
      .toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: /Clear overrides/ })).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: /Save tier map/ })).toBeInTheDocument();
    expect(within(panel).getByLabelText("Model for gbagent at cheap")).toBeInTheDocument();
    expect(within(panel).getByTestId("tier-map-grading-link")).toHaveAttribute("href", "/harness");
    expect(within(panel).getByTestId("tier-map-performance-link")).toHaveAttribute("href", "/harness");
  });
});
