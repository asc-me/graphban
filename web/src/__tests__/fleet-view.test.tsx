import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FleetView } from "@/features/fleet/FleetView";
import { settingsPath } from "@/lib/routes";

const api = vi.hoisted(() => ({
  mintFleetKey: vi.fn(),
  endWavePreview: vi.fn(),
  endWave: vi.fn(),
  dismissAgent: vi.fn(),
  revokeExpiredKeys: vi.fn(),
  revokeUnusedSeats: vi.fn(),
  revokeKey: vi.fn(),
  issueSeats: vi.fn(),
  reissueSeat: vi.fn(),
  saveFleetProfile: vi.fn(),
  clearFleetProfile: vi.fn(),
  saveFleetPolicy: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api }));

vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ active: { id: "core", name: "Core", tag: "core" },
                          activeId: "core", projects: [] }),
}));

const fleet = vi.hoisted(() => ({ data: null as unknown, refetch: vi.fn() }));
vi.mock("@/lib/queries", () => ({ useFleet: () => fleet }));

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <FleetView />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const AGENT = {
  id: "GB-A1", key: "GB-A1", label: "opus @ macbook:wt-2", active_role: "worker",
  enrolled: true, dismissed: false,
  state: "working", capabilities: {}, worktree: "~/wt-2", branch: "feat/x",
  branch_orphaned: false, last_seen_at: null, holdings: [],
};

const BASE = {
  agents: [], online: 0, total: 0, roles: ["planner", "worker", "reviewer"],
  by_role: {}, posture: "single-agent",
  presence_ttl_seconds: 150, heartbeat_interval_seconds: 50,
  review_queue: [], clusters: [], seats: [], credentials: [], waves: ["wave-1"],
  profile: null, policy: null,
};

/**
 * The onboarding controls live under the SEATS tab. The view is three answers to three
 * questions — who is out there, what seats are outstanding, which credential each agent is on
 * — so a test that provisions has to open the one that provisions.
 */
async function openWork(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /^Work/ }));
}

async function openWave(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /^Wave/ }));
}

async function openLegacyWaveKey(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /Legacy: mint a wave key/i }));
}

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

  it("says what to do when the fleet is empty", async () => {
    renderView();
    // An empty roster is the state a first-time user is in, and "no agents" alone leaves them
    // nowhere. The default posture is one agent — an API key, no seat. Sending everyone to
    // Wave taught the fleet path as the first thing, which is the rarer install.
    expect(screen.queryByText(/Mint a credential below/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Mint an API key/ }))
      .toHaveAttribute("href", settingsPath("project/api-keys"));
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Issue seats on Wave/ }));
    expect(screen.getByRole("button", { name: /Issue the seats into/ })).toBeInTheDocument();
  });

  it("labels a wave-tagged key as swept by End wave, not as an ordinary one", () => {
    // Two minting surfaces, one list: without the mark, End wave sweeping one of them
    // is invisible until it happens.
    fleet.data = {
      ...BASE,
      credentials: [
        { id: "k1", name: "yours", prefix: "gb_sk_aaaa", wave: null, revoked: false,
          posture: null, roles: [], agents: 1, expires_at: null },
        { id: "k2", name: "wave", prefix: "gb_sk_bbbb", wave: "wave-1", revoked: false,
          posture: null, roles: [], agents: 1, expires_at: null },
      ],
    };
    renderView();
    expect(screen.getByText(/wave-1 · swept by End wave/)).toBeInTheDocument();
    expect(screen.getByText(/yours · never swept/)).toBeInTheDocument();
  });

  it("sends someone looking for MCP to API keys, not a second mint on this page", () => {
    // Fleet is seats and the roster. The key that goes in an MCP config is minted on
    // Settings → API keys. Linking to /mcp-tools or the MCP catalog would send the
    // person who asked "how do I connect" to a tool list with no key on it.
    renderView();
    expect(screen.getByRole("link", { name: /looking for mcp\?/i }))
      .toHaveAttribute("href", settingsPath("project/api-keys"));
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

  it("says when an agent was last heard from, not just that it is offline", () => {
    // `offline` alone cannot tell a process that died thirty seconds ago from one gone for
    // a week, and those call for opposite responses — wait, or clean up after it.
    fleet.data = { ...BASE, total: 1, online: 0,
                   agents: [{ ...AGENT, state: "offline", holdings: [{ id: "GB-1", title: "held work", status: "in_progress" }],
                              last_seen_at: new Date(Date.now() - 3 * 3600_000).toISOString() }] };
    renderView();
    expect(screen.getByText("3h ago")).toBeInTheDocument();
  });

  it("shows what an agent is DOING with its item, not only which item", () => {
    // The status alone cannot separate writing code from waiting on CI from reworking a
    // bounce — all three are `in_progress`. The phase is derived server-side (GRPH-522)
    // precisely so it works for vendor children that report nothing.
    fleet.data = { ...BASE, total: 1, online: 1,
                   agents: [{ ...AGENT, state: "working", last_seen_at: new Date().toISOString(),
                              holdings: [{ id: "GB-1", title: "held work", status: "in_progress",
                                           phase: "integrating", phase_basis: "pr recorded",
                                           bounced: false }] }] };
    renderView();
    expect(screen.getByText("integrating")).toBeInTheDocument();
    // The basis rides along, so the inference can be checked rather than trusted.
    expect(screen.getByTitle("pr recorded")).toBeInTheDocument();
  });

  it("marks rework beside the phase instead of replacing it", () => {
    // Both facts survive: a bounced item being tested is `verifying` AND rework. Folding
    // one into the other would drop the bounce the moment the agent ran a test.
    fleet.data = { ...BASE, total: 1, online: 1,
                   agents: [{ ...AGENT, state: "working", last_seen_at: new Date().toISOString(),
                              holdings: [{ id: "GB-1", title: "held work", status: "in_progress",
                                           phase: "verifying", phase_basis: "test receipt",
                                           bounced: true }] }] };
    renderView();
    expect(screen.getByText("verifying · rework")).toBeInTheDocument();
  });

  it("does not render a dead agent's frozen item as work in flight", () => {
    // The absence-reads-as-clean case, at the last place it can still mislead: the server
    // says `stale`, and the row must not present that like an activity.
    fleet.data = { ...BASE, total: 1, online: 0,
                   agents: [{ ...AGENT, state: "offline", last_seen_at: null,
                              holdings: [{ id: "GB-1", title: "held work", status: "in_progress",
                                           phase: "stale", phase_basis: "agent offline",
                                           bounced: false }] }] };
    renderView();
    const label = screen.getByText("stale");
    expect(label).toBeInTheDocument();
    expect(label.className).toContain("italic");
    expect(screen.queryByText("building")).not.toBeInTheDocument();
  });

  it("distinguishes an agent that never reported from one that stopped", () => {
    // A registered agent with no heartbeat has not run yet. Rendering that as silence,
    // identically to one that ran and died, loses the only difference that matters.
    fleet.data = { ...BASE, total: 1, online: 0,
                   agents: [{ ...AGENT, state: "offline", holdings: [{ id: "GB-1", title: "held work", status: "in_progress" }], last_seen_at: null }] };
    renderView();
    expect(screen.getByText("no heartbeat yet")).toBeInTheDocument();
  });

  it("keeps an offline agent reachable rather than dropping it", async () => {
    // The rule TIGHTENED here, and the distinction is holding rather than presence. An offline
    // agent that holds nothing is history — two thirds of a real roster was that, so the tab
    // answering "who is here now" was mostly answering "who was ever here". It is collapsed
    // behind a toggle, never dropped: the row is still the record that the agent existed.
    fleet.data = { ...BASE, total: 1, agents: [{ ...AGENT, state: "offline" }] };
    const user = userEvent.setup();
    renderView();

    expect(screen.queryByText("opus @ macbook:wt-2")).not.toBeInTheDocument();

    await user.click(screen.getByText(/Show 1 gone/));
    expect(screen.getByText("opus @ macbook:wt-2")).toBeInTheDocument();
    expect(screen.getByText("offline")).toBeInTheDocument();
  });

  it("renders the review ban as a negative on the item", async () => {
    // "AGT-4 built it" — the refusal belongs to the ITEM. A list of who is eligible would make
    // the reader reconstruct the invariant instead of reading it.
    fleet.data = { ...BASE, review_queue: [{
      id: "i1", key: "GB-12", title: "Add the guard", branch: "feat/x",
      built_by: "GB-A1", built_by_label: "opus @ macbook", reviewed_by: null,
    }] };
    renderView();
    await openWork(userEvent.setup());
    expect(screen.getByText("opus @ macbook built it")).toBeInTheDocument();
  });

  it("says why a held-back cluster is waiting", async () => {
    // Without the reason a queued cluster looks like the fleet being stuck, and a human
    // overrides the divvy.
    fleet.data = { ...BASE, clusters: [{
      items: ["GB-13"], areas: ["backend/app/models"], predicted: false,
      held_by: "GB-A1", blocked_on: "backend/app/models",
    }] };
    renderView();
    await openWork(userEvent.setup());
    expect(screen.getByText(/queued until GB-A1 releases/)).toBeInTheDocument();
  });

  it("does not mint all-in-one as a wave key", async () => {
    // wave.ts: a wave is the specialised roles; all-in-one is an un-enrolled agent on an
    // ordinary API key. Offering it here minted a wave-tagged key that End wave swept —
    // the default install, produced as the thing the wave exists to revoke.
    const user = userEvent.setup();
    renderView();
    await openLegacyWaveKey(user);
    expect(screen.queryByRole("button", { name: "all-in-one" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /API key in Settings/ }))
      .toHaveAttribute("href", settingsPath("project/api-keys"));
    for (const r of ["planner", "worker", "reviewer"]) {
      expect(screen.getByRole("button", { name: r })).toBeInTheDocument();
    }
  });

  it("shows which specialised role is selected", async () => {
    // Selection is background/border, never ROLE_TONE. When all-in-one lived here its
    // tone was byte-identical to unselected, so choosing it looked like not choosing it.
    const user = userEvent.setup();
    renderView();
    await openLegacyWaveKey(user);

    for (const r of ["planner", "worker", "reviewer"]) {
      await user.click(screen.getByRole("button", { name: r }));
      const chosen = screen.getByRole("button", { name: r });
      expect(chosen).toHaveAttribute("aria-pressed", "true");
      for (const other of ["planner", "worker", "reviewer"].filter((x) => x !== r)) {
        const notChosen = screen.getByRole("button", { name: other });
        expect(notChosen).toHaveAttribute("aria-pressed", "false");
        const classes = (el: Element) => new Set((el.className || "").split(/\s+/).filter(Boolean));
        const a = classes(chosen);
        const b = classes(notChosen);
        expect([...a].some((c) => !b.has(c)) || [...b].some((c) => !a.has(c))).toBe(true);
      }
    }
  });

  it("mints a role-narrowed credential and shows all three pastes", async () => {
    api.mintFleetKey.mockResolvedValue({
      id: "k1", plaintext: "gb_sk_secret", role: "reviewer", wave: "wave-1", prefix: "gb_sk_ab",
    });
    const user = userEvent.setup();
    renderView();
    await openLegacyWaveKey(user);

    await user.click(screen.getByRole("button", { name: "reviewer" }));
    await user.click(screen.getByRole("button", { name: /Mint a reviewer wave key/ }));

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
    api.endWavePreview.mockResolvedValue({ keys: 2, seats: 4, agents: 1, leases: 1, reservations: 3 });
    const user = userEvent.setup();
    renderView();

    await user.click(screen.getByRole("button", { name: "End wave" }));

    // Names SEATS as well as keys now: a wave owns seats, and under enrolment they are the
    // part that actually stops the fleet. A confirm that counted only keys would understate
    // the damage on every wave issued since PRD-19.
    // Names the WAVE as well as the damage: with more than one wave in flight, "revoke 2 keys"
    // does not say whose. Matched on the paragraph's full text because the wave sits in its
    // own span — a substring query would pass on a sentence that never mentions the wave.
    const line = await screen.findByText(
      (_, el) => el?.tagName === "P"
        && /End wave-1 in core: revoke 4 seats and 2 wave-tagged keys, release 1 lease and 3 reservations\?/
          .test(el.textContent ?? ""),
    );
    expect(line).toBeInTheDocument();
    expect(screen.getByText(/release 1 lease and 3 reservations/)).toBeInTheDocument();
    // The irreversible confirm names the object Settings mints. "Credential" here is the
    // two-surfaces mix: an operator cannot tell whether their API key is the one that dies.
    expect(screen.getByText(/Your own API key is untouched/)).toBeInTheDocument();
    expect(screen.queryByText(/Your own credential is untouched/)).not.toBeInTheDocument();
    expect(api.endWave).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "End wave-1" }));
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
    await openLegacyWaveKey(user);
    await user.click(screen.getByRole("button", { name: /Mint a worker wave key/ }));
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
    await openWave(user);

    await user.click(screen.getByRole("button", { name: "End wave" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(api.endWave).not.toHaveBeenCalled();
  });

  it("lets you pick WHICH wave to end, and never shows one wave's damage against another", async () => {
    // Ending a wave is irreversible. With two waves in flight, a dialog that reads "revoke 2
    // keys" without naming whose is an invitation to end the wrong cohort — and stale counts
    // behind a new selection are worse still, because they look authoritative.
    fleet.data = {
      ...BASE, total: 1, agents: [AGENT],
      waves: ["wave-2", "wave-1"],
      seats: [{ id: "s1", role: "worker", wave: "wave-1", state: "unused", consumed_by: null,
                reissued_from: null, expires_at: null },
              { id: "s2", role: "worker", wave: "wave-2", state: "unused", consumed_by: null,
                reissued_from: null, expires_at: null }],
    };
    api.endWavePreview
      .mockResolvedValueOnce({ keys: 0, seats: 1, agents: 1, leases: 0, reservations: 0 })
      .mockResolvedValueOnce({ keys: 9, seats: 7, agents: 3, leases: 2, reservations: 1 });
    const user = userEvent.setup();
    renderView();

    await user.click(screen.getByRole("button", { name: "End wave" }));
    await screen.findByRole("button", { name: "End wave-2" });   // newest by default

    await user.click(screen.getByRole("button", { name: "wave-1" }));

    // The counts followed the selection rather than lingering from wave-2.
    const line = await screen.findByText(
      (_, el) => el?.tagName === "P" && /End wave-1 in core: revoke 7 seats and 9 wave-tagged keys/.test(el.textContent ?? ""),
    );
    expect(line).toBeInTheDocument();
    expect(api.endWavePreview).toHaveBeenLastCalledWith("core", "wave-1");

    await user.click(screen.getByRole("button", { name: "End wave-1" }));
    await waitFor(() => expect(api.endWave).toHaveBeenCalledWith("core", "wave-1"));
  });

  it("hides the previous wave's counts while a new one is being read", async () => {
    // The dangerous middle state: you pick wave-1, the request is in flight, and the dialog
    // still shows wave-2's numbers under the new label. That reads as authoritative and is
    // wrong. Needs a DEFERRED mock — with an instantly-resolving one the in-flight state
    // never exists, and a sabotage that reinstates stale counts passes unnoticed.
    fleet.data = {
      ...BASE, total: 1, agents: [AGENT],
      waves: ["wave-2", "wave-1"],
      seats: [{ id: "s1", role: "worker", wave: "wave-1", state: "unused", consumed_by: null,
                reissued_from: null, expires_at: null },
              { id: "s2", role: "worker", wave: "wave-2", state: "unused", consumed_by: null,
                reissued_from: null, expires_at: null }],
    };
    let release!: (v: unknown) => void;
    api.endWavePreview
      .mockResolvedValueOnce({ keys: 9, seats: 7, agents: 3, leases: 2, reservations: 1 })
      .mockReturnValueOnce(new Promise((r) => { release = r; }));
    const user = userEvent.setup();
    renderView();

    await user.click(screen.getByRole("button", { name: "End wave" }));
    await screen.findByText((_, el) => el?.tagName === "P" && /revoke 7 seats/.test(el.textContent ?? ""));

    await user.click(screen.getByRole("button", { name: "wave-1" }));

    // In flight: wave-2's numbers must be gone, and the destructive button unavailable.
    expect(screen.queryByText((_, el) => el?.tagName === "P" && /revoke 7 seats/.test(el.textContent ?? "")))
      .not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "End wave-1" })).toBeDisabled();

    release({ keys: 0, seats: 1, agents: 1, leases: 0, reservations: 0 });
    // A wave with no keys must not say "0 keys". Under enrolment that is every new wave, and
    // a permanent zero clause buries the number that does matter behind one that never will.
    await screen.findByText((_, el) => el?.tagName === "P" && /revoke 1 seat, release/.test(el.textContent ?? ""));
  });

  it("collapses spent seats and revoked credentials instead of listing them forever", async () => {
    // After ending a wave the seats list was 19 dead rows deep and the two that still mattered
    // were invisible in it. Spent is history: kept reachable, because the chain of who took
    // what is the audit trail, but not competing with what is live.
    const seat = (id: string, state: string) => ({
      id, role: "worker", wave: "wave-1", state, consumed_by: null,
      reissued_from: null, expires_at: null,
    });
    fleet.data = {
      ...BASE,
      seats: [seat("live", "unused"), seat("gone", "revoked"), seat("old", "expired")],
      credentials: [
        { id: "k1", name: "yours", prefix: "gb_sk_aaaa", wave: null, revoked: false,
          posture: null, roles: [], agents: 1, expires_at: null },
        { id: "k2", name: "spent", prefix: "gb_sk_bbbb", wave: "wave-1", revoked: true,
          posture: null, roles: [], agents: 0, expires_at: null },
      ],
    };
    const user = userEvent.setup();
    renderView();
    await openWave(user);

    // Asserting the TOGGLE exists proves nothing — the rows have to actually be gone. The
    // first version of this checked only the toggle, and a sabotage that re-listed every seat
    // inline passed it.
    expect(screen.queryByText("revoked")).not.toBeInTheDocument();
    expect(screen.queryByText("expired")).not.toBeInTheDocument();
    expect(screen.getByText("unused")).toBeInTheDocument();

    await user.click(screen.getByText(/Show 2 spent/));
    expect(screen.getByText("revoked")).toBeInTheDocument();
    expect(screen.getByText("expired")).toBeInTheDocument();
    // The sweep names what it will actually take: unused AND expired, never consumed.
    expect(screen.getByText(/Clear the 2 unredeemed/)).toBeInTheDocument();

    // Credentials live on the ROSTER tab now, so this is a tab switch BACK, not onward.
    await user.click(screen.getByRole("button", { name: /^Connections/ }));
    expect(screen.getByText("gb_sk_aaaa")).toBeInTheDocument();
    expect(screen.queryByText("gb_sk_bbbb")).not.toBeInTheDocument();
    await user.click(screen.getByText(/Show 1 revoked/));
    expect(screen.getByText("gb_sk_bbbb")).toBeInTheDocument();
  });

  it("offers to clear EXPIRED credentials, and never merely unused ones", async () => {
    // "Unused" is the tempting second signal and is a trap: a key minted minutes ago for a
    // machine nobody has set up yet has never been used, and sweeping on that would revoke an
    // operator's own setup before they finished it. Expiry is unambiguous.
    const cred = (id: string, expires: string | null) => ({
      id, name: id, prefix: `gb_sk_${id}`, wave: null, revoked: false,
      posture: null, roles: [], agents: 0, expires_at: expires,
    });
    fleet.data = {
      ...BASE,
      credentials: [cred("dead", "2020-01-01T00:00:00Z"), cred("fresh", null)],
    };
    const user = userEvent.setup();
    renderView();

    await user.click(screen.getByText(/Revoke the 1 expired/));

    await waitFor(() => expect(api.revokeExpiredKeys).toHaveBeenCalledWith("core"));
    // The never-used one is still listed and was not swept.
    expect(screen.getByText("gb_sk_fresh")).toBeInTheDocument();
  });

  it("hides End wave entirely when no wave owns anything", async () => {
    // The state the walk was actually in: three waves existed, none had a single live seat or
    // key between them, and the selector cheerfully offered all three. Ending history is not
    // an action — and a destructive control that is always available teaches you to ignore it.
    fleet.data = { ...BASE, total: 3, agents: [AGENT], waves: [] };
    renderView();

    expect(screen.queryByRole("button", { name: "End wave" })).not.toBeInTheDocument();
  });

  it("shows End wave as soon as one does", async () => {
    fleet.data = { ...BASE, total: 3, agents: [AGENT], waves: ["wave-4"] };
    renderView();

    expect(screen.getByRole("button", { name: "End wave" })).toBeInTheDocument();
  });

  it("collapses agents that are gone, but never one that still holds something", async () => {
    // Two thirds of the roster was dead agents holding nothing — the tab answering "who is
    // here now" was mostly answering "who was ever here".
    //
    // The line is HOLDING, not presence. "Offline agents fade rather than vanish" was written
    // for the agent that died mid-work: a lease nobody can finish, or a branch only a human
    // can resolve. Hiding THAT would be the bug this collapse must not introduce.
    const agent = (id: string, state: string, extra = {}) => ({
      ...AGENT, id, key: id, state, holdings: [], branch_orphaned: false, ...extra,
    });
    fleet.data = {
      ...BASE,
      agents: [
        agent("GB-A1", "working"),
        agent("GB-A2", "offline"),
        agent("GB-A3", "offline", { holdings: [{ id: "GB-7", title: "x", status: "in_progress" }] }),
        agent("GB-A4", "offline", { branch_orphaned: true }),
      ],
    };
    const user = userEvent.setup();
    renderView();

    expect(screen.getByText("GB-A1")).toBeInTheDocument();            // live
    expect(screen.getByText("GB-A3")).toBeInTheDocument();            // dead, holding an item
    expect(screen.getByText("GB-A4")).toBeInTheDocument();            // dead, orphaned branch
    expect(screen.queryByText("GB-A2")).not.toBeInTheDocument();      // dead, holding nothing

    await user.click(screen.getByText(/Show 1 gone/));
    expect(screen.getByText("GB-A2")).toBeInTheDocument();
  });

  it("names the tab for what it carries: connections, not just the agent roster", async () => {
    // The tab holds the agent roster AND the credentials that can reach the project. "Roster"
    // named only the first half — and when asked, the person who built this workflow read
    // "roster" as meaning the MCP configs, which is the other half. One question, one name.
    fleet.data = { ...BASE, agents: [AGENT], total: 1 };
    renderView();

    expect(screen.getByRole("button", { name: /^Connections/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Roster \(/ })).not.toBeInTheDocument();
    // The agent list inside it is still a roster — that section keeps the word that fits it.
    expect(screen.getByText("Roster")).toBeInTheDocument();
  });

  it("points at waiting review work from the other tabs, and takes you there", async () => {
    // The cost of tabbing this page is that the review queue can be buried, and a queue nobody
    // notices is a fleet quietly stalled. The count rides the tab label AND the other tabs say
    // it in a sentence — a number is easy to miss when you are looking at something else.
    fleet.data = {
      ...BASE, total: 1, agents: [AGENT],
      review_queue: [{ id: "GB-3", key: "GB-3", title: "thing", branch: "b",
                       built_by: "GB-A1", built_by_label: "opus", reviewed_by: null }],
    };
    const user = userEvent.setup();
    renderView();

    expect(screen.getByRole("button", { name: /^Work \(1 in review\)/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", {
      name: (_, el) => /1\s*items? waiting for review/.test(el.textContent ?? ""),
    }));

    // It navigated rather than merely informing.
    expect(screen.getByText("thing")).toBeInTheDocument();
    // And the pointer is gone once you are looking at the thing it pointed to.
    expect(screen.queryByRole("button", {
      name: (_, el) => /waiting for review/.test(el.textContent ?? ""),
    })).not.toBeInTheDocument();
  });

  it("says nothing when nothing is waiting", async () => {
    // A permanent banner is one you stop reading, which would cost exactly the attention this
    // is buying.
    fleet.data = { ...BASE, total: 1, agents: [AGENT], review_queue: [] };
    renderView();

    expect(screen.queryByRole("button", {
      name: (_, el) => /waiting for review/.test(el.textContent ?? ""),
    })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Work" })).toBeInTheDocument();
  });

  it("groups un-enrolled agents apart instead of mixing them into the fleet", async () => {
    // An un-enrolled agent is the single-agent posture: legitimate, and simply not a fleet.
    // Grouped BELOW rather than hidden — these are live processes doing work, not history, so
    // the disclosure is about order, not concealment.
    fleet.data = {
      ...BASE, total: 2,
      agents: [
        { ...AGENT, id: "GB-A1", key: "GB-A1", label: "seated", enrolled: true },
        { ...AGENT, id: "GB-A2", key: "GB-A2", label: "solo", enrolled: false },
      ],
    };
    renderView();

    expect(screen.getByText("seated")).toBeInTheDocument();
    expect(screen.getByText("solo")).toBeInTheDocument();
    expect(screen.getByText(/1 un-enrolled/)).toBeInTheDocument();
    expect(screen.getByText(/API key, no seat/)).toBeInTheDocument();
  });

  it("dismisses an agent, and offers to put it back", async () => {
    fleet.data = {
      ...BASE, total: 2,
      agents: [
        { ...AGENT, id: "GB-A1", key: "GB-A1", label: "live one" },
        { ...AGENT, id: "GB-A9", key: "GB-A9", label: "done with", dismissed: true },
      ],
    };
    const user = userEvent.setup();
    renderView();

    // Dismissed rows are out of the roster entirely until asked for.
    expect(screen.queryByText("done with")).not.toBeInTheDocument();
    await user.click(screen.getByText(/Show 1 dismissed/));
    expect(screen.getByText("done with")).toBeInTheDocument();

    // And the action reverses, because hiding is a view decision rather than a verdict.
    await user.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(api.dismissAgent).toHaveBeenCalledWith("GB-A9", true));

    await user.click(screen.getAllByRole("button", { name: "Dismiss" })[0]);
    await waitFor(() => expect(api.dismissAgent).toHaveBeenCalledWith("GB-A1", false));
  });

  // ---- a seat names its project (GRPH-476) --------------------------------------------

  it("says which project it is about to mint into, on the button", () => {
    // `ProjectBar` already shows the active project in the page chrome — and an operator who
    // has misread which project is active will misread it there too. This is at the point of
    // action, which is where it was missing both times seats went to the wrong project.
    fleet.data = { ...BASE, seats: [], keys: [] };
    renderView();
    fireEvent.click(screen.getByRole("button", { name: /^Wave/ }));
    expect(screen.getByRole("button", { name: /Issue the seats into core/ })).toBeInTheDocument();
  });

  it("puts the project ON the seat, not only on the page", async () => {
    // THE one that matters. A seat used to leave this page carrying a role and nothing else,
    // so a mis-mint was undetectable from the code — diagnosing it meant querying the live
    // `enrolments` table. And it is written as a CHECK the receiving agent can run, which is
    // the only step in the chain that catches this without a human noticing first.
    const user = userEvent.setup();
    api.issueSeats.mockResolvedValue({ seats: [{ id: 1, role: "worker", code: "WORKER-AAA111" }] });
    fleet.data = { ...BASE, seats: [], keys: [] };
    renderView();

    await user.click(screen.getByRole("button", { name: /^Wave/ }));
    await user.click(screen.getByRole("button", { name: "+ worker" }));
    await user.click(screen.getByRole("button", { name: /Issue the seats into core/ }));

    const block = await screen.findByText(/This seat is for the core project/);
    expect(block.textContent).toContain("get_context");
    expect(block.textContent).toContain("WORKER-AAA111");
  });

  it("labels each copied seat with the project as well as the role", async () => {
    const user = userEvent.setup();
    api.issueSeats.mockResolvedValue({ seats: [{ id: 2, role: "planner", code: "PLANNER-BBB222" }] });
    fleet.data = { ...BASE, seats: [], keys: [] };
    renderView();

    await user.click(screen.getByRole("button", { name: /^Wave/ }));
    await user.click(screen.getByRole("button", { name: "+ planner" }));
    await user.click(screen.getByRole("button", { name: /Issue the seats into core/ }));

    expect(await screen.findByText(/planner for core — prompt \+ seat, shown once/))
      .toBeInTheDocument();
  });

  // ---- handing the seats to a supervisor (GRPH-556) -----------------------------------

  async function _issueTwo(user: ReturnType<typeof userEvent.setup>) {
    api.issueSeats.mockResolvedValue({ wave: "wave-7", seats: [
      { id: 1, role: "worker", code: "WORKER-AAA111" },
      { id: 2, role: "reviewer", code: "REVIEWER-BBB222" },
    ] });
    fleet.data = { ...BASE, seats: [], keys: [] };
    renderView();
    await user.click(screen.getByRole("button", { name: /^Wave/ }));
    await user.click(screen.getByRole("button", { name: "+ worker" }));
    await user.click(screen.getByRole("button", { name: /Issue the seats into/ }));
    await screen.findByText(/worker for core/);
  }

  it("offers nothing about supervisors until seats exist", async () => {
    // An empty advanced panel is noise on the flow that is still primary. Seat mode — paste a
    // prompt into a terminal — remains the normal way to run a fleet.
    const user = userEvent.setup();
    fleet.data = { ...BASE, seats: [], keys: [] };
    renderView();
    await user.click(screen.getByRole("button", { name: /^Wave/ }));
    expect(screen.queryByText(/under a supervisor/)).not.toBeInTheDocument();
  });

  it("keeps the supervisor path collapsed", async () => {
    const user = userEvent.setup();
    await _issueTwo(user);
    expect(screen.getByText(/under a supervisor/)).toBeInTheDocument();
    expect(screen.queryByText(/seats\.txt/)).not.toBeInTheDocument();
  });

  it("hands over a seats file gbfleet can read unmodified", async () => {
    // THE point. `gbfleet up --seats-file` splits on lines and redeems each one, so the file
    // must be codes and nothing else — a role label or a comment is a code it would try to
    // consume. Before this, the codes existed only inside prose prompts.
    const user = userEvent.setup();
    await _issueTwo(user);
    await user.click(screen.getByText(/under a supervisor/));

    const block = screen.getByText((_, el) =>
      el?.tagName === "PRE" && el.textContent === "WORKER-AAA111\nREVIEWER-BBB222");
    expect(block).toBeInTheDocument();
  });

  it("fills the command in with the real wave and adapter", async () => {
    const user = userEvent.setup();
    await _issueTwo(user);
    await user.click(screen.getByText(/under a supervisor/));

    const cmd = screen.getByText((_, el) =>
      el?.tagName === "PRE" && (el.textContent ?? "").includes("gbfleet up"));
    expect(cmd.textContent).toContain("--seats-file seats.txt");
    expect(cmd.textContent).toContain("--adapter claude");
    expect(cmd.textContent).toContain("--wave wave-7");
    // THE CALL (GRPH-556 bounce). `--server \S+` matched any token, including a dummy URL
    // substituted for window.location.origin — the value SupervisorHandoff interpolates.
    expect(cmd.textContent).toContain(`--server ${window.location.origin}`);
  });

  it("only offers adapters the supervisor can resolve", async () => {
    // The UI must not advertise a vendor `gbfleet up` would refuse at startup. `codex` is
    // declared-and-unimplemented there, so it is absent here too.
    const user = userEvent.setup();
    await _issueTwo(user);
    await user.click(screen.getByText(/under a supervisor/));

    for (const a of ["claude", "cursor-agent", "gbagent", "grok"]) {
      expect(screen.getByRole("button", { name: a })).toBeInTheDocument();
    }
    expect(screen.queryByRole("button", { name: "codex" })).not.toBeInTheDocument();
  });

  it("says the supervisor needs an API key rather than a seat", async () => {
    // The first thing an operator gets wrong: seats are for the CHILDREN. Handing the
    // supervisor a seat, or a child the key, both fail confusingly.
    const user = userEvent.setup();
    await _issueTwo(user);
    await user.click(screen.getByText(/under a supervisor/));

    const note = screen.getByText(/not\s+a seat/);
    expect(note.textContent).toMatch(/API key/);
    expect(note.textContent).toMatch(/Fleet admin/);
  });

  it("points the supervisor path at the measured-model document", async () => {
    // GRPH-557. The finding is in docs/fleet-adapters.md. A pointer nobody asserts
    // is the walk-record problem again: operators never leave this panel for it.
    const user = userEvent.setup();
    await _issueTwo(user);
    await user.click(screen.getByText(/under a supervisor/));
    expect(screen.getByText("docs/fleet-adapters.md")).toBeInTheDocument();
  });

  it("says what a wave is where the wave is named", async () => {
    const user = userEvent.setup();
    fleet.data = { ...BASE, seats: [], keys: [] };
    renderView();
    await user.click(screen.getByRole("button", { name: /^Wave/ }));
    expect(screen.getByText(/branch prefix/)).toBeInTheDocument();
  });

  it("declares the project in the heading, so every control below it is scoped", () => {
    // The two sweep buttons act on the active project too. Naming it on each would be noise;
    // declaring it once above them is not.
    fleet.data = { ...BASE };
    renderView();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toMatch(/Fleet\s*core/);
  });
});


// ---- PRD-37: preferences and policy (criterion 3, the view half) ---------------------------

describe("harness preferences", () => {
  beforeEach(() => {
    api.saveFleetProfile.mockReset();
    api.saveFleetPolicy.mockReset();
    api.clearFleetProfile.mockReset();
    fleet.refetch.mockReset();
  });

  it("says plainly when no profile is recorded rather than showing an empty one as a choice", async () => {
    fleet.data = { ...BASE };
    const user = userEvent.setup();
    renderView();
    await openWave(user);
    expect(screen.getByTestId("fleet-profile-scope").textContent).toMatch(/none recorded/);
    expect(screen.getByTestId("fleet-policy-state").textContent).toMatch(/no constraint/);
  });

  it("renders the stored profile and policy and names the override scope", async () => {
    fleet.data = {
      ...BASE,
      profile: { user: "u1", project_id: "core", scope: "project", defaults: ["gbagent", "claude"],
                 weights: { cost: 1, quality: 0.25 }, excludes: ["grok"], updated_at: null },
      policy: { local_only: true, reviewer_cross_vendor: false, allowed_harnesses: ["gbagent"] },
    };
    const user = userEvent.setup();
    renderView();
    await openWave(user);
    expect(screen.getByTestId("fleet-profile-scope").textContent).toMatch(/override for core/);
    expect((screen.getByLabelText("Default harnesses") as HTMLInputElement).value).toBe("gbagent, claude");
    expect((screen.getByLabelText("Excluded harnesses") as HTMLInputElement).value).toBe("grok");
    expect((screen.getByLabelText("Weight cost") as HTMLInputElement).value).toBe("1");
    expect((screen.getByLabelText("Allowed harnesses") as HTMLInputElement).value).toBe("gbagent");
    expect((screen.getByLabelText(/Local only/) as HTMLInputElement).checked).toBe(true);
    expect(screen.getByRole("button", { name: /Remove override/ })).toBeTruthy();
  });

  it("saves the profile with parsed names and weights, then refetches the fleet read", async () => {
    fleet.data = { ...BASE };
    api.saveFleetProfile.mockResolvedValue({ scope: "default" });
    const user = userEvent.setup();
    renderView();
    await openWave(user);
    await user.type(screen.getByLabelText("Default harnesses"), "gbagent, claude");
    await user.type(screen.getByLabelText("Weight cost"), "1");
    await user.click(screen.getByRole("button", { name: /Save profile/ }));
    await waitFor(() => expect(api.saveFleetProfile).toHaveBeenCalledTimes(1));
    expect(api.saveFleetProfile.mock.calls[0][0]).toEqual({
      project_id: null, defaults: ["gbagent", "claude"], excludes: [], weights: { cost: 1 },
    });
    expect(fleet.refetch).toHaveBeenCalled();
    expect(screen.getByRole("status").textContent).toMatch(/Saved as your default/);
  });

  it("saves an override for the active project when asked, and the policy with the project id", async () => {
    fleet.data = { ...BASE };
    api.saveFleetProfile.mockResolvedValue({ scope: "project" });
    api.saveFleetPolicy.mockResolvedValue({ project_id: "core", policy: { local_only: true, reviewer_cross_vendor: false, allowed_harnesses: [] } });
    const user = userEvent.setup();
    renderView();
    await openWave(user);
    await user.click(screen.getByLabelText(/Save for core only/));
    await user.click(screen.getByRole("button", { name: /Save profile/ }));
    await waitFor(() => expect(api.saveFleetProfile).toHaveBeenCalledTimes(1));
    expect(api.saveFleetProfile.mock.calls[0][0].project_id).toBe("core");

    await user.click(screen.getByLabelText(/Local only/));
    await user.click(screen.getByRole("button", { name: /Save policy/ }));
    await waitFor(() => expect(api.saveFleetPolicy).toHaveBeenCalledTimes(1));
    expect(api.saveFleetPolicy.mock.calls[0][0]).toEqual({
      project_id: "core", local_only: true, reviewer_cross_vendor: false, allowed_harnesses: [],
    });
  });

  it("shows the server's refusal instead of pretending the save happened", async () => {
    fleet.data = { ...BASE };
    api.saveFleetProfile.mockRejectedValue(new Error("weight cost must be between 0 and 1, got 1.5"));
    const user = userEvent.setup();
    renderView();
    await openWave(user);
    await user.click(screen.getByRole("button", { name: /Save profile/ }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/between 0 and 1/));
    expect(fleet.refetch).not.toHaveBeenCalled();
  });
});
