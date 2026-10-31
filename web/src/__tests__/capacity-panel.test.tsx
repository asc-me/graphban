import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CapacityPanel } from "@/features/harness/CapacityPanel";
import type { HarnessCapacity } from "@/lib/types";

const capacity = vi.fn();
vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: { harnessCapacity: (...a: unknown[]) => capacity(...a) },
}));

const base: HarnessCapacity = {
  org_id: "o", as_of: "2026-10-31T00:00:00Z",
  review_reserve: { vendors: ["openai"], empty: false, only_unknown: false, warning: null },
  entitlements: [{
    id: "ent_1", vendor: "anthropic", kind: "subscription", plan_label: "Max", state: "unknown",
    vendor_text: "", since: null, reset_cadence: null, token_budget: null,
    week: { tokens: 0, reported: 0, unreported: 2, limit_hits: 0 },
    projected_exhaustion: { at: null, reason: "no token_budget stated" },
  }],
  utilization: {
    week_start: "", week_end: "", share_used: null, share_is_lower_bound: false, tokens_used: 0, budget: null,
    entitlements_with_budget: 0, entitlements_without_budget: 1, unreported_spends: 0,
    waves_ended_by_capacity: { count: null, note: "not recorded" },
    items_deferred_to_reset: { count: null, note: "not recorded" },
  },
};

function show() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><CapacityPanel projectId="p" /></QueryClientProvider>);
}

describe("CapacityPanel", () => {
  it("warns when the review reserve rests only on unknown, or is empty", async () => {
    capacity.mockResolvedValueOnce({ ...base, review_reserve: { vendors: ["openai"], empty: false,
      only_unknown: true, warning: "the review reserve rests only on unknown entitlements" } });
    show();
    expect(await screen.findByTestId("capacity-reserve-warning")).toHaveTextContent("only on unknown");
  });

  it("warns when nothing satisfies the review reserve", async () => {
    capacity.mockResolvedValueOnce({ ...base, review_reserve: { vendors: [], empty: true,
      only_unknown: false, warning: "no reviewer-eligible vendor has headroom: nothing satisfies the review reserve" } });
    show();
    expect(await screen.findByTestId("capacity-reserve-warning")).toHaveTextContent("nothing satisfies");
  });

  it("shows no reserve warning when it rests on an available entitlement", async () => {
    capacity.mockResolvedValueOnce(base);
    show();
    await screen.findByTestId("capacity-rollup");
    expect(screen.queryByTestId("capacity-reserve-warning")).toBeNull();
  });

  it("never renders an unknown as zero", async () => {
    capacity.mockResolvedValueOnce(base);
    show();
    const roll = await screen.findByTestId("capacity-rollup");
    expect(roll).toHaveTextContent("unknown — no entitlement states a token budget");
    expect(roll).toHaveTextContent("Waves ended by capacity: not recorded");
    const row = screen.getByTestId("capacity-row");
    expect(row).toHaveTextContent("+2 unreported");
    expect(row).toHaveTextContent("no token_budget stated");
    expect(row).toHaveTextContent("not stated");
  });

  it("shows a share with unreported spend as a floor, with the count", async () => {
    capacity.mockResolvedValueOnce({
      ...base,
      utilization: { ...base.utilization, share_used: 0.25, share_is_lower_bound: true,
        tokens_used: 250, budget: 1000, entitlements_with_budget: 1, unreported_spends: 1 },
    });
    show();
    const roll = await screen.findByTestId("capacity-rollup");
    expect(roll).toHaveTextContent("≥25% of 1000 tokens (+1 unreported)");
  });

  it("shows a fully reported share without a floor mark", async () => {
    capacity.mockResolvedValueOnce({
      ...base,
      utilization: { ...base.utilization, share_used: 0.25, tokens_used: 250, budget: 1000,
        entitlements_with_budget: 1 },
    });
    show();
    const roll = await screen.findByTestId("capacity-rollup");
    expect(roll).toHaveTextContent("25% of 1000 tokens");
    expect(roll).not.toHaveTextContent("≥");
  });

  it("renders nothing when the org is off a capacity plan", async () => {
    capacity.mockRejectedValueOnce(new Error("402"));
    const { container } = show();
    await new Promise((r) => setTimeout(r, 20));
    expect(container).toBeEmptyDOMElement();
  });
});
