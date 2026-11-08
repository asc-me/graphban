import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PerformanceGrid } from "@/features/harness/PerformanceGrid";
import type { HarnessCard, HarnessCell, HarnessReport } from "@/lib/types";

const leaf = (model: string, capability: string, size_band: string, finished: number, signed_off: number): HarnessCell => ({
  key: { vendor: "claude", model, binary_version: "1", capability, size_band },
  finished, signed_off, bounced: 0, rate: signed_off / finished, below_floor: finished < 5,
  sampling: { first_choice: finished, fallback: 0, explicit: 0, unknown: 0, probe: 0 },
  kind: "leaf", family: capability[0], label: capability, leaves: [], skew: null, median_seconds: null,
  cost: { comparable: false, reason: "none" } as never, versions_seen: [], is_current_version: true, series: [],
});

const data = {
  project_id: "core", window_days: 90, versions: "current", floor: 5, skew_share: 0.8,
  generated_at: "", below_floor_count: 0,
  cells: [
    leaf("opus", "A1", "XS", 10, 9), leaf("opus", "A1", "XL", 8, 4), leaf("opus", "B1", "M", 2, 1),
    leaf("sonnet", "A1", "M", 12, 6),
  ],
  capability_set: { leaves: ["A1", "B1"], families: { A: ["A1"], B: ["B1"] }, labels: {} },
} as unknown as HarnessReport;

const card = (rule: string, model: string, capability: string, size_band: string): HarnessCard =>
  ({
    rule, key: `${rule}:${model}:${capability}`, evidence_hash: "h", title: `${rule} title ${model}`, detail: "d",
    cells: [{ cell: { vendor: "claude", model, binary_version: "1", capability, size_band }, finished: 10, signed_off: 9, rate: 0.9 }],
    siblings: [], draft: { where: "matrix.toml", evidence_line: "line" },
    replay: { considered: 0, changed: 0, skipped_no_resolution: 0, truncated: false, moves: [], summary: "" },
    thresholds: {}, state: "new", previously: null,
  }) as unknown as HarnessCard;

const cards = [card("R1", "opus", "A1", "XS"), card("R2", "sonnet", "A1", "M"), card("R1", "opus", "B1", "M")];

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    harnessRecommendations: async () => ({ project_id: "core", cards, rules: ["R1", "R2"], lessons_drafted: [], window_days: 90, floor: 5 }),
  },
}));

function show() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <PerformanceGrid data={data} routing={[]} projectId="core" />
    </QueryClientProvider>,
  );
}

describe("Harness grid filters", () => {
  it("hide under-floor leaves no below-floor cell showing a rate", async () => {
    show();
    const user = userEvent.setup();
    expect(screen.getAllByTestId("harness-grid-cell").some((c) => c.getAttribute("data-below-floor") === "true")).toBe(true);
    await user.click(screen.getByTestId("harness-hide-under-floor"));
    for (const c of screen.queryAllByTestId("harness-grid-cell")) expect(c.getAttribute("data-below-floor")).toBe("false");
    expect(screen.getAllByTestId("harness-grid-row")).toHaveLength(2);
  });

  it("a family limits the columns, XS and XL show only their band", async () => {
    show();
    const user = userEvent.setup();
    await user.selectOptions(screen.getByTestId("harness-grid-family"), "B");
    expect(screen.getAllByTestId("harness-grid-col").map((c) => c.textContent)).toEqual(["B1"]);
    await user.selectOptions(screen.getByTestId("harness-grid-family"), "");
    await user.click(screen.getByRole("button", { name: "XS" }));
    const cells = screen.getAllByTestId("harness-grid-cell");
    expect(cells).toHaveLength(1);
    expect(cells[0]).toHaveTextContent("9/10");
    await user.click(screen.getByRole("button", { name: "XL" }));
    expect(screen.getByTestId("harness-grid-cell")).toHaveTextContent("4/8");
  });

  it("the Recommendations count equals the cards it opens, and the cell panel lists matching cards", async () => {
    show();
    const user = userEvent.setup();
    const btn = await screen.findByRole("button", { name: /^Recommendations \(3\)$/ });
    await user.click(btn);
    expect(within(screen.getByTestId("harness-grid-cards")).getAllByTestId("harness-card")).toHaveLength(3);
    await user.selectOptions(screen.getByTestId("harness-grid-family"), "B");
    expect(screen.getByRole("button", { name: "Recommendations (1)" })).toBeInTheDocument();
    expect(screen.getAllByTestId("harness-card")).toHaveLength(1);
    await user.selectOptions(screen.getByTestId("harness-grid-family"), "");

    await user.click(screen.getByRole("button", { name: /^claude sonnet A1:/ }));
    const panel = screen.getByTestId("harness-panel-change");
    expect(within(panel).getAllByTestId("harness-panel-change-card")).toHaveLength(1);
    expect(panel).toHaveTextContent("R2");
    expect(panel).toHaveTextContent("apply by hand: matrix.toml");
    await user.click(screen.getByRole("button", { name: /^claude opus B1:/ }));
    expect(screen.getByTestId("harness-panel-change")).toHaveTextContent("R1");
  });

  it("says when no rule fired on a cell", async () => {
    show();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /^claude opus A1:/ }));
    await screen.findByText(/R1 · R1 title opus/);
    await user.click(screen.getByRole("button", { name: "XL" }));
    expect(screen.getByTestId("harness-panel-change")).toHaveTextContent("No rule fired on this cell");
  });
});
