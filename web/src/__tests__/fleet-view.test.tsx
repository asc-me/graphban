import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FleetView } from "@/features/fleet/FleetView";

const api = vi.hoisted(() => ({
  mintFleetKey: vi.fn(),
  endWavePreview: vi.fn(),
  endWave: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api }));

vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ active: { id: "core" }, activeId: "core", projects: [] }),
}));

const fleet = vi.hoisted(() => ({ data: null as unknown, refetch: vi.fn() }));
vi.mock("@/lib/queries", () => ({ useFleet: () => fleet }));

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <FleetView />
    </QueryClientProvider>,
  );
}

const AGENT = {
  id: "GB-A1", key: "GB-A1", label: "opus @ macbook:wt-2", active_role: "worker",
  state: "working", capabilities: {}, worktree: "~/wt-2", branch: "feat/x",
  branch_orphaned: false, last_seen_at: null, holdings: [],
};

const BASE = {
  agents: [], online: 0, total: 0, roles: ["planner", "worker", "reviewer"],
  by_role: {}, posture: "single-agent",
  presence_ttl_seconds: 150, heartbeat_interval_seconds: 50,
  review_queue: [], clusters: [],
};

/**
 * D5's job is to make a fleet usable — without it every wave costs a trip through Settings
 * and three hand-assembled pastes per terminal. These pin the parts that carry meaning rather
 * than the layout.
 */
describe("Fleet view", () => {
  beforeEach(() => {
    fleet.data = { ...BASE };
    api.mintFleetKey.mockReset();
    api.endWavePreview.mockReset();
    api.endWave.mockReset();
  });

  it("says what to do when the fleet is empty", () => {
    renderView();
    // An empty roster is the state a first-time user is in, and "no agents" alone leaves them
    // nowhere. The instruction is the content.
    expect(screen.getByText(/Mint a credential below/)).toBeInTheDocument();
  });

  it("breaks the count down by role rather than totalling it", () => {
    // "4 agents online" is the same number for a balanced fleet and for four workers with
    // nobody to review them — and those need opposite actions.
    fleet.data = { ...BASE, total: 4, online: 4, posture: "fleet",
                   by_role: { planner: 1, worker: 2, reviewer: 1, "all-in-one": 0 },
                   agents: [AGENT] };
    renderView();
    expect(screen.getByText("2 worker")).toBeInTheDocument();
    expect(screen.getByText("1 reviewer")).toBeInTheDocument();
    expect(screen.getByText(/the fleet reviews itself/)).toBeInTheDocument();
  });

  it("names the single-agent posture rather than showing it as a role", () => {
    // The DEFAULT deployment. Showing it as a worker would misdescribe the commonest case —
    // and imply a server-side review gate that deliberately does not apply here.
    fleet.data = { ...BASE, total: 1, online: 1, posture: "single-agent",
                   by_role: { planner: 0, worker: 0, reviewer: 0, "all-in-one": 1 },
                   agents: [{ ...AGENT, active_role: "all-in-one" }] };
    renderView();
    expect(screen.getByText("1 all-in-one")).toBeInTheDocument();
    expect(screen.getByText(/you are the reviewer/)).toBeInTheDocument();
    expect(screen.queryByText(/1 worker/)).not.toBeInTheDocument();
  });

  it("omits roles nobody holds", () => {
    fleet.data = { ...BASE, total: 1, online: 1, posture: "fleet",
                   by_role: { planner: 0, worker: 1, reviewer: 0 }, agents: [AGENT] };
    renderView();
    expect(screen.getByText("1 worker")).toBeInTheDocument();
    expect(screen.queryByText("0 planner")).not.toBeInTheDocument();
  });

  it("shows an orphaned branch on the agent's row", () => {
    // The fleet released the ITEM by itself. The BRANCH is state only a human can resolve, so
    // it cannot live in a log.
    fleet.data = { ...BASE, total: 1, online: 1,
                   agents: [{ ...AGENT, state: "offline", branch_orphaned: true }] };
    renderView();
    expect(screen.getByText("branch orphaned")).toBeInTheDocument();
  });

  it("keeps offline agents on the roster rather than hiding them", () => {
    fleet.data = { ...BASE, total: 1, agents: [{ ...AGENT, state: "offline" }] };
    renderView();
    expect(screen.getByText("opus @ macbook:wt-2")).toBeInTheDocument();
    expect(screen.getByText("offline")).toBeInTheDocument();
  });

  it("renders the review ban as a negative on the item", () => {
    // "AGT-4 built it" — the refusal belongs to the ITEM. A list of who is eligible would make
    // the reader reconstruct the invariant instead of reading it.
    fleet.data = { ...BASE, review_queue: [{
      id: "i1", key: "GB-12", title: "Add the guard", branch: "feat/x",
      built_by: "GB-A1", built_by_label: "opus @ macbook", reviewed_by: null,
    }] };
    renderView();
    expect(screen.getByText("opus @ macbook built it")).toBeInTheDocument();
  });

  it("says why a held-back cluster is waiting", () => {
    // Without the reason a queued cluster looks like the fleet being stuck, and a human
    // overrides the divvy.
    fleet.data = { ...BASE, clusters: [{
      items: ["GB-13"], areas: ["backend/app/models"], predicted: false,
      held_by: "GB-A1", blocked_on: "backend/app/models",
    }] };
    renderView();
    expect(screen.getByText(/queued until GB-A1 releases/)).toBeInTheDocument();
  });

  it("mints a role-narrowed credential and shows all three pastes", async () => {
    api.mintFleetKey.mockResolvedValue({
      id: "k1", plaintext: "gb_sk_secret", role: "reviewer", wave: "wave-1", prefix: "gb_sk_ab",
    });
    const user = userEvent.setup();
    renderView();

    await user.click(screen.getByRole("button", { name: "reviewer" }));
    await user.click(screen.getByRole("button", { name: /Mint a reviewer credential/ }));

    await waitFor(() => expect(api.mintFleetKey).toHaveBeenCalledWith(
      expect.objectContaining({ role: "reviewer", project_id: "core" })));
    expect(await screen.findByText(/1. Key/)).toBeInTheDocument();
    expect(screen.getByText(/2. Connect/)).toBeInTheDocument();
    expect(screen.getByText(/3. Prime/)).toBeInTheDocument();
  });

  it("names the damage before ending a wave, and only acts on confirm", async () => {
    // "Are you sure?" teaches people to click through. "Revoke 2 keys, release 1 lease?" is a
    // decision — and the count has to arrive BEFORE anything is destroyed.
    fleet.data = { ...BASE, total: 1, agents: [AGENT] };
    api.endWavePreview.mockResolvedValue({ keys: 2, agents: 1, leases: 1, reservations: 3 });
    const user = userEvent.setup();
    renderView();

    await user.click(screen.getByRole("button", { name: "End wave" }));

    expect(await screen.findByText(/Revoke 2 keys/)).toBeInTheDocument();
    expect(screen.getByText(/release 1 lease and 3 reservations/)).toBeInTheDocument();
    expect(api.endWave).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "End the wave" }));
    await waitFor(() => expect(api.endWave).toHaveBeenCalledWith("core", "wave-1"));
  });

  it("cancelling the confirm destroys nothing", async () => {
    fleet.data = { ...BASE, total: 1, agents: [AGENT] };
    api.endWavePreview.mockResolvedValue({ keys: 1, agents: 1, leases: 0, reservations: 0 });
    const user = userEvent.setup();
    renderView();

    await user.click(screen.getByRole("button", { name: "End wave" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(api.endWave).not.toHaveBeenCalled();
  });
});
