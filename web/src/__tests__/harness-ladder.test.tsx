import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Ladder } from "@/features/harness/Ladder";
import { RoutingReason } from "@/features/harness/RoutingReason";
import type { HarnessLadder, HarnessLadderRung, HarnessRoutingReason } from "@/lib/types";

const ladderRead = vi.fn();
const profileRead = vi.fn();
const save = vi.fn();
const routing = vi.fn();
vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    harnessLadder: (...a: unknown[]) => ladderRead(...a),
    fleetProfile: (...a: unknown[]) => profileRead(...a),
    saveFleetProfile: (...a: unknown[]) => save(...a),
    harnessRouting: (...a: unknown[]) => routing(...a),
  },
}));

const rung = (r: string, removed = false): HarnessLadderRung => ({ rung: r, label: r, description: `${r} desc`, removed });
const ladder = (o: Partial<HarnessLadder> = {}): HarnessLadder => ({
  project_id: "p",
  policy: [rung("subscription"), rung("local"), rung("api_key", true)],
  override: null,
  effective: [rung("subscription"), rung("local")],
  refused: [],
  ...o,
});
const profile = { default: null, override: null, profile: { defaults: ["claude"], weights: {}, excludes: [], budget_tokens: null, mix: null } };

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  profileRead.mockResolvedValue(profile);
  save.mockResolvedValue({});
});

describe("Fallback ladder", () => {
  it("marks policy-removed rungs REMOVED BY PROJECT POLICY, and offers no control to add them back", async () => {
    ladderRead.mockResolvedValue(ladder());
    wrap(<Ladder projectId="p" />);
    const pol = await screen.findByTestId("ladder-policy");
    expect(within(pol).getByText("REMOVED BY PROJECT POLICY")).toBeInTheDocument();
    const row = within(screen.getByTestId("ladder-override")).getAllByTestId("ladder-override-rung")
      .find((r) => r.getAttribute("data-rung") === "api_key")!;
    expect(within(row).getByText("Can't add back")).toBeInTheDocument();
    expect(within(row).queryByRole("button")).toBeNull();
    expect(screen.queryAllByTestId("ladder-restore")).toHaveLength(0);
  });

  it("Remove, Restore and Match policy send the expected PUT body", async () => {
    ladderRead.mockResolvedValue(ladder());
    wrap(<Ladder projectId="p" />);
    const user = userEvent.setup();
    await user.click((await screen.findAllByTestId("ladder-remove"))[0]);
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(save).toHaveBeenLastCalledWith({
      project_id: "p", defaults: ["claude"], weights: {}, excludes: [], budget_tokens: null, mix: null, ladder: ["local"],
    });

    ladderRead.mockResolvedValue(ladder({ override: [rung("local")] }));
    wrap(<Ladder projectId="p" />);
    const restore = await screen.findByTestId("ladder-restore");
    await user.click(restore);
    await waitFor(() => expect(save).toHaveBeenCalledTimes(2));
    expect(save.mock.calls[1][0].ladder).toEqual(["local", "subscription"]);

    await user.click(screen.getAllByTestId("ladder-match-policy")[0]);
    await waitFor(() => expect(save).toHaveBeenCalledTimes(3));
    expect(save.mock.calls[2][0].ladder).toBeNull();
  });

  it("shows a server refusal verbatim and leaves the ladder as it was", async () => {
    ladderRead.mockResolvedValue(ladder());
    save.mockRejectedValueOnce(new Error(JSON.stringify({ detail: "ladder must be a non-empty list of rungs" })));
    wrap(<Ladder projectId="p" />);
    await userEvent.setup().click((await screen.findAllByTestId("ladder-remove"))[0]);
    expect(await screen.findByTestId("ladder-refusal")).toHaveTextContent("ladder must be a non-empty list of rungs");
    expect(within(screen.getByTestId("ladder-override")).getAllByTestId("ladder-remove")).toHaveLength(2);
  });
});

describe("Routing reason", () => {
  it("shows the stored why verbatim and the skip trace only as a PLANNED tag", async () => {
    const r: HarnessRoutingReason = {
      item_key: "GRPH-1270", stage: "build", claimant: "alex", at: "2026-11-04T14:18:00Z", reason: null,
      latest: { verdict: "claimed", entitlement_id: "ent.a", why: "ent.a has headroom, ranked first" },
      attempts: [{ kind: "claim", entitlement_id: "ent.a", reason: "ent.a has headroom, ranked first", agent_id: null,
        agent_vendor: null, agent_model: null, matched: true, reserved_until: null, ended_reason: null }],
    };
    routing.mockResolvedValue(r);
    wrap(<RoutingReason defaultItem="GRPH-1270" />);
    expect(await screen.findByTestId("routing-why")).toHaveTextContent("“ent.a has headroom, ranked first”");
    expect(routing).toHaveBeenCalledWith("GRPH-1270");
    expect(screen.getByTestId("routing-head")).toHaveTextContent("GRPH-1270 · build · claimed by alex");
    expect(screen.getByTestId("routing-chosen")).toHaveTextContent("ent.a");
    expect(screen.getByTestId("routing-planned")).toHaveTextContent("PLANNED · skip trace");
    expect(screen.getAllByTestId("routing-attempt")).toHaveLength(1);
  });

  it("an item never routed shows the server's reason", async () => {
    routing.mockResolvedValue({ item_key: "GRPH-1", stage: "next", claimant: null, attempts: [], latest: null,
      reason: "never routed by capacity", at: null });
    wrap(<RoutingReason defaultItem="GRPH-1" />);
    expect(await screen.findByTestId("routing-never")).toHaveTextContent("never routed by capacity");
  });
});
