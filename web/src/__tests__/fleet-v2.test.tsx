import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
vi.mock("@/lib/queries", () => ({
  useFleet: () => fleet,
  useConfig: () => ({ data: { hosted_mode: false } }),
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
});
