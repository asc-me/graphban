import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
  routing_decisions: { window_start: "", window_end: "", groups: [], reason: null },
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
const openEnts = async () => userEvent.click(await screen.findByTestId("capacity-view-ents"));
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
    await openEnts();
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
    await openEnts();
    expect(await screen.findByTestId("capacity-exhausted")).toHaveTextContent("“Limit reached” · seen");
  });

  it("broken shows the reason, the reporter and the declarers", async () => {
    const r = (label: string) => ({ agent_id: label, label, worktree: "" });
    capacity.mockResolvedValueOnce(base([ent({ state: "broken", vendor_text: "401 invalid_api_key",
      reported_by: r("rn-ci-01"), declared_by: [r("rn-ci-01"), r("rn-ci-02")] })]));
    show();
    await openEnts();
    expect(screen.getByTestId("capacity-runner")).toHaveTextContent(
      "“401 invalid_api_key” · reported by rn-ci-01 · declared by rn-ci-01, rn-ci-02");
  });

  it("unknown says it was never measured", async () => {
    capacity.mockResolvedValueOnce(base([ent({ state: "unknown" })]));
    show();
    await openEnts();
    expect(await screen.findByTestId("capacity-unknown")).toHaveTextContent("Never measured. Ranked below available.");
  });

  it("unreachable names the runner and its last known state", async () => {
    capacity.mockResolvedValueOnce(base([ent({ state: "unreachable", last_known_state: "exhausted", runner: { label: "rn-priya", owner: null } })]));
    show();
    await openEnts();
    const el = screen.getByTestId("capacity-unreachable");
    expect(el).toHaveTextContent("rn-priya has gone quiet · last known EXHAUSTED");
    expect(el).not.toHaveTextContent("PLANNED");
  });

  it("the 7-day column is the reported total, and a not-recorded rollup reads not recorded, never 0", async () => {
    capacity.mockResolvedValueOnce(base([ent({ week: { tokens: 18_000, reported: 4, unreported: 1, limit_hits: 0 } })]));
    show();
    await openEnts();
    const row = screen.getByTestId("capacity-row");
    expect(within(row).getByTestId("capacity-week")).toHaveTextContent("18k+1 unreported");
    expect(row).not.toHaveTextContent("PLANNED");
    expect(screen.getByTestId("capacity-rollup")).toHaveTextContent("Waves ended by capacity: not recorded");
  });

  it("shows runs out ~time when projected, else the reason", async () => {
    capacity.mockResolvedValueOnce(base([ent({ token_budget: 100, projected_exhaustion: { at: "2026-11-24T00:00:00Z", reason: "" } })]));
    show();
    await openEnts();
    expect(screen.getByTestId("capacity-row")).toHaveTextContent("runs out ~");
  });

  it("routing decisions show count, verbatim reasons and the no-decisions reason", async () => {
    const d = base();
    d.routing_decisions = { window_start: "", window_end: "", reason: "no claims routed in window", groups: [
      { verdict: "held", label: "Held", count: 1, note: null, recent: [
        { item_key: "GRPH-9", entitlement_id: "ent.a", reason: "reserved for review", at: "2026-10-31T00:00:00Z" }] },
      { verdict: "deferred", label: "Deferred", count: null, recent: [], note: "not recorded: x" }] };
    capacity.mockResolvedValueOnce(d);
    show();
    expect(await screen.findByTestId("capacity-routing-reason")).toHaveTextContent("no claims routed in window");
    const [held, deferred] = screen.getAllByTestId("capacity-routing-group");
    expect(within(held).getByTestId("capacity-routing-count")).toHaveTextContent("1 · 7d");
    expect(within(held).getByTestId("capacity-routing-row")).toHaveTextContent("“reserved for review”");
    expect(within(deferred).getByTestId("capacity-routing-count")).toHaveTextContent("not recorded");
  });

  it("reserve shows the exact server warning, never the healthy line", async () => {
    const d = base();
    d.review_reserve.warning = "No review headroom for anthropic builds.";
    capacity.mockResolvedValueOnce(d);
    show();
    expect(await screen.findByTestId("capacity-reserve-warning-text")).toHaveTextContent(/^No review headroom for anthropic builds\.$/);
    expect(screen.getByTestId("capacity-reserve-warning")).toHaveTextContent("△ SERVER WARNING");
    expect(screen.queryByTestId("capacity-reserve-healthy")).toBeNull();
  });

  it("reserve healthy line only when warning is null and both lists are empty", async () => {
    capacity.mockResolvedValueOnce(base());
    show();
    expect(await screen.findByTestId("capacity-reserve-healthy")).toHaveTextContent("Review reserve held by a measured entitlement");
  });

  it("reserve marks only_unknown_for and empty_for vendors and withholds the healthy line", async () => {
    const d = base();
    d.review_reserve.only_unknown_for = ["openai"];
    d.review_reserve.empty_for = ["google"];
    capacity.mockResolvedValueOnce(d);
    show();
    expect(await screen.findByTestId("capacity-reserve-unknown")).toHaveTextContent("○ openai");
    expect(screen.getByTestId("capacity-reserve-empty")).toHaveTextContent("◒ google");
    expect(screen.queryByTestId("capacity-reserve-healthy")).toBeNull();
  });

  it("an unknown-only reserve is not the healthy state", async () => {
    const d = base();
    d.review_reserve.only_unknown_for = ["openai"];
    capacity.mockResolvedValueOnce(d);
    show();
    expect(await screen.findByTestId("capacity-reserve-unknown")).toHaveTextContent("○ openai");
    expect(screen.queryByTestId("capacity-reserve-healthy")).toBeNull();
  });
});
