import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CapacityPanel } from "@/features/harness/CapacityPanel";
import { RoutingMode } from "@/features/harness/RoutingMode";
import type { HarnessCapacity, HarnessCapacityEntitlement, HarnessLadder, HarnessLadderRung } from "@/lib/types";

const capacity = vi.fn();
const ladderRead = vi.fn();
const policyRead = vi.fn();
const policySave = vi.fn();
const me = vi.fn();
const routing = vi.fn();

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    harnessCapacity: (...a: unknown[]) => capacity(...a),
    harnessLadder: (...a: unknown[]) => ladderRead(...a),
    fleetPolicy: (...a: unknown[]) => policyRead(...a),
    saveFleetPolicy: (...a: unknown[]) => policySave(...a),
    me: () => me(),
    harnessRouting: (...a: unknown[]) => routing(...a),
  },
}));

const rung = (r: string, removed = false): HarnessLadderRung => ({
  rung: r, label: r, description: `${r} desc`, removed,
});

const sub = (id: string, vendor: string): HarnessCapacityEntitlement => ({
  id,
  vendor,
  kind: "subscription",
  plan_label: "pro",
  state: "available",
  vendor_text: "",
  since: null,
  seen_at: null,
  runner: { label: "r", owner: null },
  reported_by: null,
  declared_by: [],
  token_budget: null,
  daily_tokens: [1, 1, 1, 1, 1, 1, 1],
  week: { tokens: 7, reported: 7, unreported: 0, limit_hits: 0 },
  projected_exhaustion: { at: null, reason: "no burn", basis: "" },
  reset_cadence: "weekly",
});

const emptyCapacity = (ents: HarnessCapacityEntitlement[] = []): HarnessCapacity => ({
  org_id: "o",
  org: { name: "acme", entitlement_count: ents.length },
  as_of: "2026-10-31T00:00:00Z",
  review_reserve: { vendors: [], empty: true, empty_for: [], only_unknown: false, only_unknown_for: [], warning: null },
  entitlements: ents,
  routing_decisions: { window_start: "", window_end: "", groups: [], reason: null },
  utilization: {
    week_start: "", week_end: "", share_used: null, share_is_lower_bound: false, tokens_used: 0, budget: null,
    entitlements_with_budget: 0, entitlements_without_budget: 0, unreported_spends: 0,
    waves_ended_by_capacity: { count: null, note: "not recorded" },
    items_deferred_to_reset: { count: null, note: "not recorded" },
  },
});

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  me.mockResolvedValue({ name: "Alex" });
  routing.mockResolvedValue({ item_key: "", stage: "", claimant: null, attempts: [], latest: null, reason: "none", at: null });
  ladderRead.mockResolvedValue({
    project_id: "p",
    policy: [rung("subscription"), rung("api_key"), rung("local")],
    override: null,
    effective: [rung("subscription"), rung("api_key"), rung("local")],
    refused: [],
  } satisfies HarnessLadder);
});

describe("Capacity routing mode", () => {
  it("default mode shows the in-force rule and does not offer replay, cheapest, or hold", async () => {
    policyRead.mockResolvedValue({
      project_id: "p",
      policy: null,
      writable: true,
    });
    capacity.mockResolvedValue(emptyCapacity([sub("s1", "anthropic"), sub("s2", "openai")]));
    wrap(<CapacityPanel projectId="p" />);
    await screen.findByTestId("routing-rule-in-force");
    expect(screen.getByTestId("routing-mode-next")).toBeChecked();
    expect(screen.queryByText(/replay/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/cheapest/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/hold-until-reset/i)).not.toBeInTheDocument();
  });

  it("with custom hierarchy off, shows the three-rung ladder and soonest-reset rule together", async () => {
    policyRead.mockResolvedValue({
      project_id: "p",
      policy: null,
      writable: true,
    });
    capacity.mockResolvedValue(emptyCapacity([sub("s1", "anthropic")]));
    wrap(<CapacityPanel projectId="p" />);
    await screen.findByTestId("capacity-ladder");
    const rule = await screen.findByTestId("routing-rule-in-force");
    expect(rule).toHaveTextContent(/soonest reset/i);
    const rungs = screen.getAllByTestId("ladder-policy-rung");
    expect(rungs.map((r) => r.getAttribute("data-rung"))).toEqual(["subscription", "api_key", "local"]);
  });

  it("custom hierarchy Apply persists subscription order on policy", async () => {
    const user = userEvent.setup();
    policyRead.mockResolvedValue({
      project_id: "p",
      policy: { local_only: false, reviewer_cross_vendor: false, allowed_harnesses: [], routing_mode: "next_available" },
      writable: true,
    });
    policySave.mockImplementation(async (body: unknown) => ({ project_id: "p", policy: body }));
    wrap(<RoutingMode projectId="p" entitlements={[sub("s-first", "a"), sub("s-second", "b")]} />);
    await user.click(await screen.findByTestId("routing-mode-custom"));
    const rows = await screen.findAllByTestId("routing-subscription-row");
    await user.click(within(rows[1]).getByLabelText("Move up"));
    await user.click(screen.getByTestId("routing-apply"));
    await waitFor(() => expect(policySave).toHaveBeenCalledTimes(1));
    const body = policySave.mock.calls[0][0] as { routing_mode: string; subscription_order: string[] };
    expect(body.routing_mode).toBe("custom_hierarchy");
    expect(body.subscription_order[0]).toBe("s-second");
  });

  it("Discard leaves saved policy unchanged", async () => {
    const user = userEvent.setup();
    policyRead.mockResolvedValue({
      project_id: "p",
      policy: {
        local_only: false, reviewer_cross_vendor: false, allowed_harnesses: [],
        routing_mode: "custom_hierarchy", subscription_order: ["s-a", "s-b"],
      },
      writable: true,
    });
    wrap(<RoutingMode projectId="p" entitlements={[sub("s-a", "a"), sub("s-b", "b")]} />);
    await user.click(await screen.findByTestId("routing-mode-next"));
    await user.click(screen.getByTestId("routing-discard"));
    expect(screen.getByTestId("routing-mode-custom")).toBeChecked();
    expect(policySave).not.toHaveBeenCalled();
  });

  it("read-only caller cannot Apply", async () => {
    policyRead.mockResolvedValue({
      project_id: "p",
      policy: { local_only: false, reviewer_cross_vendor: false, allowed_harnesses: [] },
      writable: false,
    });
    wrap(<RoutingMode projectId="p" entitlements={[sub("s1", "x")]} />);
  await screen.findByTestId("routing-apply-denied");
    expect(screen.getByTestId("routing-apply")).toBeDisabled();
  });

  it("says when local is unreachable on the ladder", async () => {
    policyRead.mockResolvedValue({ project_id: "p", policy: null, writable: true });
    const local: HarnessCapacityEntitlement = {
      ...sub("local-1", "ollama"),
      kind: "local",
      state: "unreachable",
    };
    wrap(<RoutingMode projectId="p" entitlements={[local]} />);
    expect(await screen.findByTestId("capacity-local-unreachable")).toHaveTextContent(/local is unreachable/i);
  });
});
