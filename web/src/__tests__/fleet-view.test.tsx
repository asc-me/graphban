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

  it("offers all-in-one beside the three roles", async () => {
    // The roster REPORTS all-in-one, so the page must be able to create one — otherwise it
    // names a posture the reader has no way to produce.
    renderView();
    for (const r of ["planner", "worker", "reviewer", "all-in-one"]) {
      expect(screen.getByRole("button", { name: r })).toBeInTheDocument();
    }
  });

  it("shows which role is selected — including all-in-one", async () => {
    // The bug: the selected style came from ROLE_TONE, and all-in-one's tone was
    // `text-muted border-line-2` — the SAME classes as unselected. Choosing it looked
    // identical to not choosing it, and three credentials were minted all-in-one before
    // anyone noticed. Asserted via aria-pressed so it cannot regress into a colour question.
    const user = userEvent.setup();
    renderView();

    for (const r of ["planner", "worker", "reviewer", "all-in-one"]) {
      await user.click(screen.getByRole("button", { name: r }));
      const chosen = screen.getByRole("button", { name: r });
      expect(chosen).toHaveAttribute("aria-pressed", "true");
      for (const other of ["planner", "worker", "reviewer", "all-in-one"].filter((x) => x !== r)) {
        const notChosen = screen.getByRole("button", { name: other });
        expect(notChosen).toHaveAttribute("aria-pressed", "false");
        // And it must LOOK different. `aria-pressed` alone would not have caught the original
        // defect, which was a pure class collision — selected all-in-one rendered
        // `text-muted border-line-2`, unselected rendered `border-line-2 text-muted`.
        // Compared as SETS so class order never makes this pass by accident.
        const classes = (el: Element) => new Set((el.className || "").split(/\s+/).filter(Boolean));
        const a = classes(chosen);
        const b = classes(notChosen);
        expect([...a].some((c) => !b.has(c)) || [...b].some((c) => !a.has(c))).toBe(true);
      }
    }
  });

  it("mints an unnarrowed credential for all-in-one, and primes it as the solo posture", async () => {
    api.mintFleetKey.mockResolvedValue({
      id: "k1", plaintext: "gb_sk_secret", role: "all-in-one", wave: "wave-1", prefix: "gb_sk_ab",
    });
    const user = userEvent.setup();
    renderView();

    await user.click(screen.getByRole("button", { name: "all-in-one" }));
    await user.click(screen.getByRole("button", { name: /Mint an all-in-one credential/ }));

    await waitFor(() => expect(api.mintFleetKey).toHaveBeenCalledWith(
      expect.objectContaining({ role: "all-in-one" })));
    // Primed as the DEFAULT posture: no reviewer agent, the human reviews. Priming it like a
    // worker would imply a gate that deliberately does not apply here.
    expect(await screen.findByText(/the human reviews your work/)).toBeInTheDocument();
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
    expect(screen.getByText(/3. Prime/)).toBeInTheDocument();
    // The Connect step is the SHARED generator from Settings, not a local stub. The first
    // version here handed the `claude mcp add` command to Codex, Grok and opencode alike —
    // so this asserts the real per-client picker is present rather than any snippet at all.
    expect(screen.getByText(/Connect an agent · MCP/)).toBeInTheDocument();
    for (const client of ["Claude Code", "Codex", "Cursor", "opencode", "Grok CLI"]) {
      expect(screen.getByRole("button", { name: client })).toBeInTheDocument();
    }
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

  it("gives each client its own connect snippet, not one command for all", async () => {
    // THE bug from the first real walk: a two-branch stub handed `claude mcp add` to Codex,
    // Grok and opencode alike, so three of five clients were told to run a command that does
    // not exist for them. An acceptance criterion of D5 is "no hand-edited config".
    api.mintFleetKey.mockResolvedValue({
      id: "k1", plaintext: "gb_sk_secret", role: "worker", wave: "wave-1", prefix: "gb_sk_ab",
    });
    const user = userEvent.setup();
    const { container } = renderView();
    await user.click(screen.getByRole("button", { name: /Mint a worker credential/ }));
    await screen.findByText(/Connect an agent · MCP/);

    const seen = new Set<string>();
    for (const client of ["Claude Code", "Codex", "Cursor", "opencode"]) {
      await user.click(screen.getByRole("button", { name: client }));
      // McpInstall's own <pre>, not the Key or Prime blocks beside it — those correctly do
      // not vary by client, and reading one of them made this assert nothing.
      const el = container.querySelector("pre.max-h-56");
      seen.add(el?.textContent ?? "");
    }

    expect(seen.size).toBe(4);
    expect([...seen].filter((s) => s.includes("claude mcp add"))).toHaveLength(1);
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
