import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CapacityPanel } from "@/features/harness/CapacityPanel";
import { consoleHref } from "@/lib/routes";
import type { HarnessCapacity, HarnessCapacityEntitlement } from "@/lib/types";

const capacity = vi.fn();
vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: { harnessCapacity: (...a: unknown[]) => capacity(...a) },
}));

const ent = (o: Partial<HarnessCapacityEntitlement> = {}): HarnessCapacityEntitlement => ({
  id: "ent.a", vendor: "anthropic", kind: "subscription", plan_label: "Max", state: "available",
  vendor_text: "", since: null, runner: { label: "rn-1", owner: null },
  daily_tokens: [null, 5, 0, 10, null, 2, 1], seen_at: null, reset_cadence: "5h rolling", token_budget: null,
  reported_by: null, declared_by: [],
  week: { tokens: 18, reported: 4, unreported: 0, limit_hits: 0 },
  projected_exhaustion: { at: null, reason: "no token_budget stated" }, ...o,
});
const base = (entitlements = [ent()]): HarnessCapacity => ({
  org_id: "o1", org: { name: "acme", entitlement_count: entitlements.length }, as_of: "2026-10-31T00:00:00Z",
  review_reserve: { vendors: [], empty: false, empty_for: [], only_unknown: false, only_unknown_for: [], warning: null },
  entitlements,
  utilization: {
    week_start: "2026-10-17T00:00:00Z", week_end: "2026-10-24T00:00:00Z", share_used: null, share_is_lower_bound: false,
    tokens_used: 0, budget: null, entitlements_with_budget: 0, entitlements_without_budget: entitlements.length,
    unreported_spends: 0,
    waves_ended_by_capacity: { count: null, note: "not recorded" },
    items_deferred_to_reset: { count: null, note: "not recorded" },
  },
});

function show() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><CapacityPanel projectId="p" /></QueryClientProvider>);
}
const ENT_HREF = consoleHref("/orgs/acme/settings/entitlements");

describe("Capacity tab", () => {
  it("on a 402 shows the server message and hint verbatim, with an attach link", async () => {
    capacity.mockRejectedValueOnce(new Error(JSON.stringify({
      detail: "capacity routing needs one of: team, enterprise; this project has no org",
      rule_id: null, hint: "attach this deployment to a cloud org on team or enterprise, or install a license" })));
    show();
    expect(await screen.findByTestId("capacity-refusal-message")).toHaveTextContent(
      "capacity routing needs one of: team, enterprise; this project has no org");
    expect(screen.getByTestId("capacity-refusal-hint")).toHaveTextContent(
      "attach this deployment to a cloud org on team or enterprise, or install a license");
    expect(screen.getByTestId("capacity-attach")).toHaveAttribute("href", consoleHref("/orgs"));
  });

  it("points Manage entitlements and Declare one at the console", async () => {
    capacity.mockResolvedValueOnce(base([]));
    show();
    expect(await screen.findByTestId("capacity-org")).toHaveTextContent("acme · 0 entitlements");
    expect(screen.getByTestId("capacity-manage")).toHaveAttribute("href", ENT_HREF);
    expect(screen.getByTestId("capacity-empty")).toHaveTextContent("○ NO ENTITLEMENTS DECLARED");
    expect(screen.getByTestId("capacity-declare")).toHaveAttribute("href", ENT_HREF);
  });

  it("with no completed week says not enough data", async () => {
    capacity.mockResolvedValueOnce(base([]));
    show();
    expect(await screen.findByTestId("capacity-rollup")).toHaveTextContent(
      "○ NOT ENOUGH DATA “no completed week since capacity routing was enabled”");
  });

  it("exhausted shows the vendor quote and when it was seen", async () => {
    capacity.mockResolvedValueOnce(base([ent({ state: "exhausted", vendor_text: "Limit reached", seen_at: "2026-10-31T13:55:00Z" })]));
    show();
    expect(await screen.findByTestId("capacity-exhausted")).toHaveTextContent("“Limit reached” · seen");
  });

  it("broken shows the reason, the reporter and the declarers", async () => {
    const r = (label: string) => ({ agent_id: label, label, worktree: "" });
    capacity.mockResolvedValueOnce(base([ent({ state: "broken", vendor_text: "401 invalid_api_key",
      reported_by: r("rn-ci-01"), declared_by: [r("rn-ci-01"), r("rn-ci-02")] })]));
    show();
    expect(await screen.findByTestId("capacity-runner")).toHaveTextContent(
      "“401 invalid_api_key” reported by rn-ci-01 · declared by rn-ci-01, rn-ci-02");
  });

  it("unknown says it was never measured", async () => {
    capacity.mockResolvedValueOnce(base([ent({ state: "unknown" })]));
    show();
    expect(await screen.findByTestId("capacity-unknown")).toHaveTextContent("Never measured. Ranked below available.");
  });

  it("unreachable names the runner and tags the last-known state as planned", async () => {
    capacity.mockResolvedValueOnce(base([ent({ state: "unreachable", runner: { label: "rn-priya", owner: null } })]));
    show();
    const el = await screen.findByTestId("capacity-unreachable");
    expect(el).toHaveTextContent("rn-priya has gone quiet.");
    expect(el).toHaveTextContent("PLANNED · last-known state");
  });

  it("draws a null day as a gap and a not-recorded rollup as not recorded, never 0", async () => {
    capacity.mockResolvedValueOnce(base([ent({ daily_tokens: [null, 5, 0, 10, null, 2, 1] })]));
    show();
    const row = await screen.findByTestId("capacity-row");
    expect(within(row).getAllByTestId("capacity-spark-gap")).toHaveLength(2);
    expect(within(row).getAllByTestId("capacity-spark-bar")).toHaveLength(5);
    expect(row).toHaveTextContent("PLANNED · forecast reset");
    expect(screen.getByTestId("capacity-rollup")).toHaveTextContent("Waves ended by capacity: not recorded");
  });

  it("shows runs out ~time when projected, else the reason", async () => {
    capacity.mockResolvedValueOnce(base([ent({ token_budget: 100, projected_exhaustion: { at: "2026-11-24T00:00:00Z", reason: "" } })]));
    show();
    expect(await screen.findByTestId("capacity-row")).toHaveTextContent("runs out ~");
  });
});
