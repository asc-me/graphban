import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PerformanceGrid } from "@/features/harness/PerformanceGrid";
import type { HarnessCard, HarnessCell, HarnessReport } from "@/lib/types";

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

const card = (rule: string, vendor: string, model: string, capability: string, size_band: string): HarnessCard =>
  ({
    rule,
    key: `${rule}:${model}:${capability}`,
    evidence_hash: "h",
    title: `${rule} title ${model}`,
    detail: "d",
    cells: [
      {
        cell: { vendor, model, binary_version: "1", capability, size_band },
        finished: 10,
        signed_off: 9,
        rate: 0.9,
      },
    ],
    siblings: [],
    draft: { where: "matrix.toml", evidence_line: "line" },
    replay: { considered: 0, changed: 0, skipped_no_resolution: 0, truncated: false, moves: [], summary: "" },
    thresholds: {},
    state: "new",
    previously: null,
  }) as unknown as HarnessCard;

const cards = [
  card("R1", "claude", "opus", "A1", "S"),
  card("R2", "anthropic", "sonnet", "A1", "M"),
  card("R1", "claude", "opus", "B1", "M"),
];

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    harnessRecommendations: async () => ({
      project_id: "core",
      cards,
      rules: ["R1", "R2"],
      lessons_drafted: [],
      window_days: 90,
      floor: 5,
    }),
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

const effortBtn = (band: string) =>
  screen.getAllByTestId("harness-effort-filter").find((b) => b.getAttribute("data-effort") === band)!;

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

  it("a vendor filter narrows the rows", async () => {
    show();
    const user = userEvent.setup();
    expect(screen.getAllByTestId("harness-grid-row")).toHaveLength(2);
    await user.selectOptions(screen.getByTestId("harness-grid-vendor"), "anthropic");
    expect(screen.getAllByTestId("harness-grid-row")).toHaveLength(1);
    expect(screen.getByTestId("harness-grid-row")).toHaveTextContent("sonnet");
    await user.selectOptions(screen.getByTestId("harness-grid-vendor"), "claude");
    expect(screen.getAllByTestId("harness-grid-row")).toHaveLength(1);
    await user.selectOptions(screen.getByTestId("harness-grid-harness"), "claude\u0000opus");
    expect(screen.getByTestId("harness-grid-row")).toHaveTextContent("opus");
  });

  it("a family column expands to its leaves and shows the served leaf label", async () => {
    show();
    const user = userEvent.setup();
    expect(screen.getAllByTestId("harness-grid-col").map((c) => c.textContent)).toEqual(
      expect.arrayContaining(["Afamily · expand"]),
    );
    await user.click(screen.getByRole("button", { name: /A.*family · expand/ }));
    expect(screen.getAllByTestId("harness-grid-col").some((c) => c.textContent?.includes("A1Localised fix"))).toBe(true);
    await user.selectOptions(screen.getByTestId("harness-grid-family"), "B");
    expect(screen.getAllByTestId("harness-grid-col").map((c) => c.textContent)).toEqual(["Bfamily · expand"]);
    await user.selectOptions(screen.getByTestId("harness-grid-family"), "");
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

    await user.click(screen.getByRole("button", { name: /A.*family · expand/ }));
    const sonnetA1 = await screen.findByRole("button", { name: /^anthropic sonnet A1:/i });
    await user.click(sonnetA1);
    const panel = await screen.findByTestId("harness-panel-change");
    expect(within(panel).getAllByTestId("harness-panel-change-card")).toHaveLength(1);
    expect(panel).toHaveTextContent("R2");
    expect(panel).toHaveTextContent("apply by hand: matrix.toml");
    await user.click(screen.getByRole("button", { name: /^claude opus A1:/ }));
    expect(screen.getByTestId("harness-panel-change")).toHaveTextContent("R1");
  });

  it("says when no rule fired on a cell", async () => {
    show();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /A.*family · expand/ }));
    await user.click(effortBtn("L"));
    const lCell = screen.getAllByTestId("harness-grid-cell").find((c) => c.textContent?.includes("4/8"));
    await user.click(lCell!);
    expect(screen.getByTestId("harness-panel-change")).toHaveTextContent("No rule fired on this cell");
  });
});
