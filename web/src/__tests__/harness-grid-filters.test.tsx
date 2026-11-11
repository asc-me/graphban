import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { PerformanceGrid } from "@/features/harness/PerformanceGrid";
import type { HarnessCell, HarnessReport } from "@/lib/types";

const leaf = (
  vendor: string,
  model: string,
  capability: string,
  size_band: string,
  finished: number,
  signed_off: number,
): HarnessCell => ({
  key: { vendor, model, binary_version: "1", capability, size_band },
  finished,
  signed_off,
  bounced: 0,
  rate: signed_off / finished,
  below_floor: finished < 5,
  sampling: { first_choice: finished, fallback: 0, explicit: 0, unknown: 0, probe: 0 },
  kind: "leaf",
  family: capability[0],
  label: capability,
  leaves: [],
  skew: null,
  median_seconds: null,
  cost: { comparable: false, reason: "none" } as never,
  versions_seen: [],
  is_current_version: true,
  series: [],
});

const fam = (leaves: HarnessCell[]): HarnessCell => {
  const family = leaves[0].family!;
  const finished = leaves.reduce((s, l) => s + l.finished, 0);
  const signed_off = leaves.reduce((s, l) => s + l.signed_off, 0);
  return {
    ...leaves[0],
    kind: "family",
    family,
    label: "family rollup",
    key: { ...leaves[0].key, capability: family },
    finished,
    signed_off,
    rate: signed_off / finished,
    below_floor: finished < 5,
    leaves,
  };
};

const data = {
  project_id: "core",
  window_days: 90,
  versions: "current",
  floor: 5,
  skew_share: 0.8,
  generated_at: "",
  below_floor_count: 0,
  cells: [
    fam([leaf("claude", "opus", "A1", "S", 10, 9), leaf("claude", "opus", "A1", "L", 8, 4)]),
    fam([leaf("claude", "opus", "B1", "M", 2, 1)]),
    fam([leaf("anthropic", "sonnet", "A1", "M", 12, 6)]),
  ],
  capability_set: {
    leaves: ["A1", "B1"],
    families: { A: ["A1"], B: ["B1"] },
    labels: { A1: "Localised fix", B1: "Layer leaf" },
  },
} as unknown as HarnessReport;

const routing = [
  { function: "A1", label: "Localised fix", effort_band: "S", verdict: "PREFER", confidence: "high" as const,
    attempts: 10, signed_off: 9, rate: 0.9, pick: "claude:opus", evidence: "90% · 9/10",
    fallback: null, fallback_rate: null, cheaper: false },
  { function: "A1", label: "Localised fix", effort_band: "M", verdict: "AVOID", confidence: "high" as const,
    attempts: 12, signed_off: 6, rate: 0.5, pick: "anthropic:sonnet", evidence: "50% · 6/12",
    fallback: null, fallback_rate: null, cheaper: false },
];

function show() {
  return render(<PerformanceGrid data={data} routing={routing} />);
}

const effortBtn = (band: string) =>
  screen.getAllByTestId("harness-effort-filter").find((b) => b.getAttribute("data-effort") === band)!;

/** Open a facet's popover and toggle one option. */
async function pick(user: ReturnType<typeof userEvent.setup>, facet: string, option: string) {
  await user.click(screen.getByTestId(`harness-filter-${facet}`));
  const group = screen.getByRole("group", { name: facet[0].toUpperCase() + facet.slice(1) });
  await user.click(within(group).getByRole("menuitemcheckbox", { name: new RegExp(option) }));
  await user.click(screen.getByTestId(`harness-filter-${facet}`));
}

describe("Harness grid filters", () => {
  it("hide below-floor blanks cells and explains a dash", async () => {
    show();
    const user = userEvent.setup();
    expect(screen.getAllByTestId("harness-grid-cell").some((c) => c.getAttribute("data-below-floor") === "true")).toBe(true);
    await user.click(screen.getByTestId("harness-hide-below-floor"));
    expect(screen.getByTestId("harness-hide-below-floor-note")).toHaveTextContent(/too few attempts, not none/);
    for (const c of screen.queryAllByTestId("harness-grid-cell")) expect(c.getAttribute("data-below-floor")).toBe("false");
    expect(screen.getAllByTestId("harness-grid-row")).toHaveLength(2);
  });

  it("the harness filter is multi-select and Clear restores every pair", async () => {
    show();
    const user = userEvent.setup();
    expect(screen.getAllByTestId("harness-grid-row")).toHaveLength(2);
    await pick(user, "harness", "anthropic");
    expect(screen.getAllByTestId("harness-grid-row")).toHaveLength(1);
    expect(screen.getByTestId("harness-grid-row")).toHaveTextContent("sonnet");
    expect(screen.getByTestId("harness-filter-harness")).toHaveTextContent("anthropic");
    await pick(user, "harness", "claude");
    expect(screen.getAllByTestId("harness-grid-row")).toHaveLength(2);
    expect(screen.getByTestId("harness-filter-harness")).toHaveTextContent("2 selected");
    await user.click(screen.getByTestId("harness-filter-clear"));
    expect(screen.getByTestId("harness-filter-harness")).toHaveTextContent("All");
  });

  it("filters by model family and by vendor, derived from the model name", async () => {
    show();
    const user = userEvent.setup();
    await pick(user, "family", "opus");
    expect(screen.getAllByTestId("harness-grid-row")).toHaveLength(1);
    expect(screen.getByTestId("harness-grid-row")).toHaveTextContent("opus");
    expect(screen.getByTestId("harness-filter-clear")).toHaveTextContent("1 of 2 pairs");
    await user.click(screen.getByTestId("harness-filter-clear"));
    // sonnet and opus are both Anthropic models, whatever harness ran them.
    await pick(user, "vendor", "Anthropic");
    expect(screen.getAllByTestId("harness-grid-row")).toHaveLength(2);
  });

  it("a family keeps its summary column and slides its functions in beside it", async () => {
    show();
    const user = userEvent.setup();
    expect(screen.getAllByTestId("harness-grid-col").map((c) => c.getAttribute("data-family") ?? c.getAttribute("data-leaf"))).toEqual(["A", "B"]);
    await user.click(screen.getByRole("button", { name: /^Expand A into 1 leaf/ }));
    expect(screen.getAllByTestId("harness-grid-col").map((c) => c.getAttribute("data-family") ?? c.getAttribute("data-leaf"))).toEqual(["A", "A1", "B"]);
    expect(screen.getAllByTestId("harness-grid-col")[1]).toHaveTextContent("Localised fix");
    await user.click(screen.getByRole("button", { name: /^Collapse A/ }));
    expect(screen.getAllByTestId("harness-grid-col")).toHaveLength(2);
  });

  it("the panel says what gbfleet is told, per band, from the served routing", async () => {
    show();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /^Expand A into/ }));
    await user.click(screen.getByRole("button", { name: /^claude opus A1:/ }));
    const told = screen.getByTestId("harness-panel-told");
    expect(told).toHaveTextContent("Cited as PREFER for Localised fix · S — 90% · 9/10.");
    expect(told).toHaveTextContent("Not the pick for Localised fix · M: gbfleet is told AVOID anthropic:sonnet (50% · 6/12).");
    // S before M, whatever order the rows arrive in.
    expect(told.textContent!.indexOf("· S")).toBeLessThan(told.textContent!.indexOf("· M"));
    await user.click(effortBtn("M"));
    await user.click(screen.getByRole("button", { name: /^anthropic sonnet A1:/ }));
    expect(screen.getByTestId("harness-panel-told")).toHaveTextContent("Cited as AVOID for Localised fix · M");
    expect(screen.getByTestId("harness-panel-told")).not.toHaveTextContent("· S");
  });

  it("a family cell's panel says gbfleet routes by function, not by family", async () => {
    show();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /^claude opus A family rollup:/ }));
    expect(screen.getByTestId("harness-panel-told")).toHaveTextContent(/Not cited as a family.*its one function\./);
    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("harness-cell-panel")).not.toBeInTheDocument();
  });
});
