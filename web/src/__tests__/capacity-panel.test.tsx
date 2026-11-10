import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CapacityPanel } from "@/features/harness/CapacityPanel";
import type { HarnessCapacity } from "@/lib/types";

const capacity = vi.fn();
vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: { harnessCapacity: (...a: unknown[]) => capacity(...a) },
}));

const base: HarnessCapacity = {
  org_id: "o", org: { name: "acme", entitlement_count: 1 }, as_of: "2026-10-31T00:00:00Z",
  review_reserve: { vendors: ["anthropic", "openai"], empty: false, empty_for: [], only_unknown: false,
    only_unknown_for: [], warning: null },
  entitlements: [{
    id: "ent_1", vendor: "anthropic", kind: "subscription", plan_label: "Max", state: "unknown",
    vendor_text: "", since: null, runner: { label: null, owner: null },
    daily_tokens: [null, null, null, null, null, null, null], seen_at: null, reset_cadence: null, token_budget: null,
    reported_by: null, declared_by: [],
    week: { tokens: 0, reported: 0, unreported: 2, limit_hits: 0 },
    projected_exhaustion: { at: null, reason: "no token_budget stated" },
  }],
  routing_decisions: { window_start: "", window_end: "", groups: [], reason: null },
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
    capacity.mockResolvedValueOnce({ ...base, review_reserve: { vendors: ["anthropic", "openai"],
      empty: false, empty_for: [], only_unknown: true, only_unknown_for: ["anthropic"],
      warning: "the review reserve for builds on anthropic rests only on unknown entitlements" } });
    show();
    expect(await screen.findByTestId("capacity-reserve-warning")).toHaveTextContent("only on unknown");
  });

  it("warns when nothing satisfies the review reserve", async () => {
    capacity.mockResolvedValueOnce({ ...base, review_reserve: { vendors: ["anthropic"],
      empty: true, empty_for: ["anthropic"], only_unknown: false, only_unknown_for: [],
      warning: "nothing satisfies the review reserve for builds on anthropic: no other reviewer-eligible vendor has headroom" } });
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
    expect(within(row).getByTestId("capacity-budget")).toHaveTextContent("no burn-down");
    expect(within(row).getByTestId("capacity-projection")).toHaveTextContent("no token_budget stated");
    expect(row).toHaveTextContent("not stated");
  });

  it("draws a null day in daily_tokens as a gap, not a zero-height bar", async () => {
    capacity.mockResolvedValueOnce({
      ...base,
      entitlements: [{
        ...base.entitlements[0],
        daily_tokens: [100, null, 50, null, null, 10, 20],
        token_budget: 1000,
        projected_exhaustion: { at: "2026-11-20T12:00:00Z", reason: "" },
      }],
    });
    show();
    const row = await screen.findByTestId("capacity-row");
    expect(within(row).getAllByTestId("capacity-spark-gap")).toHaveLength(3);
    const bars = within(row).getAllByTestId("capacity-spark-bar");
    expect(bars).toHaveLength(4);
    bars.forEach((bar) => expect(Number(bar.getAttribute("data-tokens"))).toBeGreaterThan(0));
  });

  it("shows projected_exhaustion.at when the server computed one", async () => {
    capacity.mockResolvedValueOnce({
      ...base,
      entitlements: [{
        ...base.entitlements[0],
        token_budget: 500,
        projected_exhaustion: { at: "2026-12-01T00:00:00Z", reason: "" },
      }],
    });
    show();
    const row = await screen.findByTestId("capacity-row");
    expect(within(row).getByTestId("capacity-projection")).toHaveTextContent("runs out ~");
  });

  it("shows projected_exhaustion.reason when at is null", async () => {
    capacity.mockResolvedValueOnce({
      ...base,
      entitlements: [{
        ...base.entitlements[0],
        token_budget: 500,
        projected_exhaustion: { at: null, reason: "insufficient trailing spend" },
      }],
    });
    show();
    const row = await screen.findByTestId("capacity-row");
    expect(within(row).getByTestId("capacity-projection")).toHaveTextContent("insufficient trailing spend");
    expect(within(row).getByTestId("capacity-projection")).not.toHaveTextContent("runs out");
  });

  it("names an entitlement without token_budget as having no burn-down, not a flat zero line", async () => {
    capacity.mockResolvedValueOnce(base);
    show();
    const row = await screen.findByTestId("capacity-row");
    expect(within(row).getByTestId("capacity-budget")).toHaveTextContent("no burn-down");
    expect(within(row).queryAllByTestId("capacity-spark-bar")).toHaveLength(0);
    expect(within(row).getAllByTestId("capacity-spark-gap")).toHaveLength(7);
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

  it("names the runner that reported a broken entitlement", async () => {
    capacity.mockResolvedValueOnce({
      ...base,
      entitlements: [{
        ...base.entitlements[0], state: "broken", vendor_text: "not logged in",
        reported_by: { agent_id: "a1", label: "mac-mini", worktree: "/w" },
        declared_by: [{ agent_id: "a1", label: "mac-mini", worktree: "/w" },
                      { agent_id: "a2", label: "", worktree: "" }],
      }],
    });
    show();
    const runner = await screen.findByTestId("capacity-runner");
    expect(runner).toHaveTextContent("reported by mac-mini · declared by mac-mini, a2");
  });
});
