import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CapacityPanel } from "@/features/harness/CapacityPanel";
import type { HarnessBurnDown, HarnessCapacity, HarnessCapacityEntitlement } from "@/lib/types";

const capacity = vi.fn();
vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: { harnessCapacity: (...a: unknown[]) => capacity(...a) },
}));

const entitlement: HarnessCapacityEntitlement = {
  id: "ent_1", vendor: "anthropic", kind: "subscription", plan_label: "Max", state: "unknown",
  vendor_text: "", since: null, runner: { label: null, owner: null },
  daily_tokens: [null, null, null, null, null, null, null], seen_at: null, reset_cadence: null, token_budget: null,
  reported_by: null, declared_by: [],
  week: { tokens: 0, reported: 0, unreported: 2, limit_hits: 0 },
  projected_exhaustion: { at: null, reason: "no token_budget stated" },
};

const base: HarnessCapacity = {
  org_id: "o", org: { name: "acme", entitlement_count: 1 }, as_of: "2026-10-31T00:00:00Z",
  review_reserve: { vendors: ["anthropic", "openai"], empty: false, empty_for: [], only_unknown: false,
    only_unknown_for: [], warning: null },
  entitlements: [entitlement],
  routing_decisions: { window_start: "", window_end: "", groups: [], reason: null },
  utilization: {
    week_start: "", week_end: "", share_used: null, share_is_lower_bound: false, tokens_used: 0, budget: null,
    entitlements_with_budget: 0, entitlements_without_budget: 1, unreported_spends: 0,
    waves_ended_by_capacity: { count: null, note: "not recorded" },
    items_deferred_to_reset: { count: null, note: "not recorded" },
  },
};

type Placed = Extract<HarnessBurnDown, { available: true }>;
const burn = (o: Partial<Placed["forecast"]> = {}, top: Partial<Placed> = {}): HarnessBurnDown => ({
  available: true, period: "monthly", step: "day", start: "2026-11-01T00:00:00Z", end: "2026-12-01T12:00:00Z",
  budget: 20_000_000, spent: 6_400_000, left: 13_600_000, unreported: 0,
  series: [{ at: "2026-11-01T00:00:00Z", left: 20_000_000 }, { at: "2026-11-09T14:00:00Z", left: 13_600_000 }],
  forecast: { verdict: "on_track", recent_steps: 3, recent_rate: 400_000, avg_rate: 750_000,
    runs_out_at: null, left_at_reset: 4_000_000, stopped_at: null, ...o },
  ...top,
});

function show() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><CapacityPanel projectId="p" /></QueryClientProvider>);
}
const openView = async (v: "ents" | "routing" | "overview") =>
  userEvent.click(await screen.findByTestId(`capacity-view-${v}`));
const tile = (key: string) => screen.getAllByTestId("capacity-summary-tile").find((t) => t.dataset.key === key)!;

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
    await openView("ents");
    const row = screen.getByTestId("capacity-row");
    expect(within(row).getByTestId("capacity-week")).toHaveTextContent("—+2 unreported");
    expect(within(row).getByTestId("capacity-budget")).toHaveTextContent("no budget stated");
    expect(within(row).getByTestId("capacity-projection")).toHaveTextContent("no token_budget stated");
    expect(within(row).getByTestId("capacity-reset")).toHaveTextContent("no reset stated");
  });

  it("opens on Overview: one burn-down card per placed period, a chip naming why for the rest", async () => {
    capacity.mockResolvedValueOnce({ ...base, entitlements: [
      { ...entitlement, id: "ent.key", kind: "api_key", state: "available", token_budget: 20_000_000, burn_down: burn() },
      { ...entitlement, id: "ent.sub", burn_down: { available: false, reason: "reset cadence '5h rolling' is not daily, weekly or monthly, so the period cannot be placed" } },
    ] });
    show();
    const card = await screen.findByTestId("capacity-burn");
    expect(card).toHaveAttribute("data-entitlement", "ent.key");
    expect(card).toHaveTextContent("14Mleft of 20M");
    expect(within(card).getByTestId("capacity-burn-verdict")).toHaveTextContent("On track — resets with ~4.0M unused");
    expect(card).toHaveTextContent("last 3 days 400k/day · avg 750k/day");
    expect(within(card).getByTestId("capacity-burn-forecast")).toBeInTheDocument();
    const chip = screen.getByTestId("capacity-no-budget-chip");
    expect(chip).toHaveTextContent("ent.sub");
    expect(chip).toHaveTextContent("'5h rolling' is not daily, weekly or monthly");
    expect(screen.queryByTestId("capacity-row")).toBeNull();
  });

  it("words each server verdict without grading it again", async () => {
    capacity.mockResolvedValueOnce({ ...base, entitlements: [
      { ...entitlement, id: "a", state: "available", burn_down: burn({ verdict: "runs_out_early", runs_out_at: "2026-11-24T12:00:00Z", left_at_reset: 0 }) },
      { ...entitlement, id: "b", state: "broken", vendor_text: "401", burn_down: burn({ verdict: "stopped", stopped_at: "2026-11-05T09:12:00Z" }) },
      { ...entitlement, id: "c", state: "available", burn_down: burn({ verdict: "no_spend" }) },
    ] });
    show();
    const cards = await screen.findAllByTestId("capacity-burn");
    const verdict = (i: number) => within(cards[i]).getByTestId("capacity-burn-verdict");
    expect(verdict(0)).toHaveTextContent("Runs out ~Nov 24 — 7 days before reset");
    expect(within(cards[0]).getByTestId("capacity-burn-out")).toBeInTheDocument();
    expect(verdict(1)).toHaveTextContent(/^Burn stopped at .* — spawns are failing, not spending\.$/);
    expect(within(cards[1]).queryByTestId("capacity-burn-forecast")).toBeNull();
    expect(cards[1]).toHaveTextContent("before failing");
    expect(verdict(2)).toHaveTextContent("No reported spend this period");
    expect(within(tile("RUNS OUT EARLY")).getByTestId("capacity-summary-value")).toHaveTextContent(/^1$/);
    expect(screen.getByTestId("capacity-view-overview")).toHaveTextContent("1 early");
  });

  it("an unreported spawn in the period is a count beside the figure, not a zero spend", async () => {
    capacity.mockResolvedValueOnce({ ...base, entitlements: [
      { ...entitlement, state: "available", burn_down: burn({}, { unreported: 3 }) }] });
    show();
    expect(await screen.findByTestId("capacity-burn-unreported")).toHaveTextContent("+3 unreported");
  });

  it("sorts the table worst first and shows budget left with its bar", async () => {
    capacity.mockResolvedValueOnce({ ...base, entitlements: [
      { ...entitlement, id: "ok", state: "available", token_budget: 20_000_000, burn_down: burn(), reset_cadence: "monthly" },
      { ...entitlement, id: "gone", state: "unreachable", last_known_state: "available" },
      { ...entitlement, id: "bad", state: "broken", vendor_text: "401" },
    ] });
    show();
    await openView("ents");
    const rows = screen.getAllByTestId("capacity-row");
    expect(rows.map((r) => r.dataset.state)).toEqual(["broken", "unreachable", "available"]);
    expect(within(rows[1]).getByTestId("capacity-unreachable")).toHaveTextContent("last known AVAILABLE");
    expect(within(rows[2]).getByTestId("capacity-budget")).toHaveTextContent("14M of 20M");
    expect(within(rows[2]).getByTestId("capacity-budget-bar")).toHaveStyle({ width: "68%" });
    expect(within(rows[2]).getByTestId("capacity-reset")).toHaveTextContent(/^monthly · Dec 1/);
  });

  it("states the owner's next reset when no period is placed", async () => {
    capacity.mockResolvedValueOnce({ ...base, entitlements: [
      { ...entitlement, reset_cadence: "5h rolling", next_reset_at: "2026-11-09T19:00:00Z",
        burn_down: { available: false, reason: "x" } }] });
    show();
    await openView("ents");
    expect(screen.getByTestId("capacity-reset")).toHaveTextContent(/^5h rolling · /);
  });

  it("an older server without next_reset_at, last_known_state or items_routed says so", async () => {
    capacity.mockResolvedValueOnce({ ...base,
      utilization: { ...base.utilization, entitlements_with_budget: 1 },
      entitlements: [{ ...entitlement, state: "unreachable", reset_cadence: "weekly", runner: { label: "rn-p", owner: null } }] });
    show();
    await openView("ents");
    expect(screen.getByTestId("capacity-unreachable")).toHaveTextContent("rn-p has gone quiet · last known state not served");
    expect(screen.getByTestId("capacity-reset")).toHaveTextContent("weekly · next reset not stated");
    expect(tile("ROUTED LAST WEEK")).toHaveTextContent("not served by this server");
  });

  it("shows projected_exhaustion when an older server sends no burn_down", async () => {
    capacity.mockResolvedValueOnce({ ...base, entitlements: [{ ...entitlement, token_budget: 500,
      projected_exhaustion: { at: "2026-12-01T00:00:00Z", reason: "" } }] });
    show();
    await openView("ents");
    expect(screen.getByTestId("capacity-projection")).toHaveTextContent("runs out ~");
  });

  it("shows a share with unreported spend as a floor, with the count", async () => {
    capacity.mockResolvedValueOnce({
      ...base,
      utilization: { ...base.utilization, share_used: 0.25, share_is_lower_bound: true,
        tokens_used: 250, budget: 1000, entitlements_with_budget: 1, unreported_spends: 1 },
    });
    show();
    const roll = await screen.findByTestId("capacity-rollup");
    expect(roll).toHaveTextContent("≥25% of 1.0k tokens (+1 unreported)");
  });

  it("shows a fully reported share without a floor mark, and the items routed", async () => {
    capacity.mockResolvedValueOnce({
      ...base,
      utilization: { ...base.utilization, share_used: 0.25, tokens_used: 250, budget: 1000,
        entitlements_with_budget: 1, items_routed: 142, week_start: "2026-10-26T12:00:00Z", week_end: "2026-11-02T12:00:00Z" },
    });
    show();
    const roll = await screen.findByTestId("capacity-rollup");
    expect(roll).toHaveTextContent("25% of 1.0k tokens");
    expect(roll).not.toHaveTextContent("≥");
    expect(within(tile("ROUTED LAST WEEK")).getByTestId("capacity-summary-value")).toHaveTextContent(/^142$/);
    expect(tile("ROUTED LAST WEEK")).toHaveTextContent("items · Oct 26 – Nov 2");
  });

  it("names the runner that reported a broken entitlement", async () => {
    capacity.mockResolvedValueOnce({
      ...base,
      entitlements: [{
        ...entitlement, state: "broken", vendor_text: "not logged in",
        reported_by: { agent_id: "a1", label: "mac-mini", worktree: "/w" },
        declared_by: [{ agent_id: "a1", label: "mac-mini", worktree: "/w" },
                      { agent_id: "a2", label: "", worktree: "" }],
      }],
    });
    show();
    await openView("ents");
    expect(screen.getByTestId("capacity-runner")).toHaveTextContent("reported by mac-mini · declared by mac-mini, a2");
  });
});
