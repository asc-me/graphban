import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { HarnessView } from "@/features/harness/HarnessView";
import { ProjectProvider } from "@/features/ProjectContext";
import type { HarnessCell, HarnessReport } from "@/lib/types";

function cell(over: Partial<HarnessCell> = {}): HarnessCell {
  return {
    key: {
      vendor: "gbagent", model: "qwen3.6", binary_version: "0.9.1", lane: "backend",
      tier: "cheap", task_class: "general", size_band: "M",
    },
    finished: 12, signed_off: 9, bounced: 3, rate: 0.75, below_floor: false,
    sampling: { first_choice: 6, fallback: 4, explicit: 2, unknown: 0 },
    skew: null, median_seconds: 620,
    cost: { comparable: true, reported: 12, finished: 12, tokens_per_signed_off: 8100,
            tokens_in: 70000, tokens_out: 2900 },
    versions_seen: ["0.9.1"], is_current_version: true,
    series: [
      { week: "2026-W35", finished: 2, signed_off: 1, rate: 0.5, below_floor: true, median_seconds: 700 },
      { week: "2026-W36", finished: 10, signed_off: 8, rate: 0.8, below_floor: false, median_seconds: 600 },
    ],
    ...over,
  };
}

function report(over: Partial<HarnessReport> = {}): HarnessReport {
  return {
    project_id: "core", window_days: 90, versions: "current", floor: 5, skew_share: 0.8,
    generated_at: new Date().toISOString(), cells: [cell()], below_floor_count: 0, ...over,
  };
}

const harness = vi.fn(async () => report());

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [{ id: "core", name: "Core", tag: "GRPH" }]),
    harness: (...args: unknown[]) => harness(...(args as [])),
  },
}));

function show() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/harness"]}>
        <ProjectProvider>
          <HarnessView />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Harness page", () => {
  it("shows a rate with the sample count that earns it", async () => {
    show();
    expect(await screen.findByTestId("harness-rate")).toHaveTextContent("75%");
    expect(screen.getByText(/9\/12 signed off/)).toBeInTheDocument();
    expect(screen.getByTestId("harness-sampling")).toHaveTextContent(
      "first choice 6 · fallback 4 · explicit 2 · unknown 0",
    );
  });

  it("draws a thin cell as below the floor and says how far it has to go", async () => {
    harness.mockResolvedValueOnce(
      report({ cells: [cell({ finished: 3, signed_off: 2, rate: 0.667, below_floor: true })],
               below_floor_count: 1 }),
    );
    show();
    expect(await screen.findByTestId("harness-below-floor")).toHaveTextContent(
      "below the floor — 3 of 5 attempts",
    );
    expect(screen.getByTestId("harness-floor-note")).toHaveTextContent("1 of 1 cells");
  });

  it("badges a lopsided cell without changing its rate", async () => {
    harness.mockResolvedValueOnce(
      report({
        cells: [cell({
          finished: 10, signed_off: 9, rate: 0.9,
          sampling: { first_choice: 9, fallback: 0, explicit: 1, unknown: 0 },
          skew: { reason: "first_choice", share: 0.9 },
        })],
      }),
    );
    show();
    expect(await screen.findByTestId("harness-skew")).toHaveTextContent(
      "sampled by preference — 90% first choice",
    );
    // The badge is a warning about comparability, not a correction: ten attempts, nine wins.
    expect(screen.getByTestId("harness-rate")).toHaveTextContent("90%");
    expect(screen.getByText(/9\/10 signed off/)).toBeInTheDocument();
  });

  it("shows no badge when the sampling is mixed", async () => {
    show();
    expect(await screen.findByTestId("harness-cell")).toBeInTheDocument();
    expect(screen.queryByTestId("harness-skew")).not.toBeInTheDocument();
  });

  it("states why a cost proxy will not compare instead of printing a zero", async () => {
    harness.mockResolvedValueOnce(
      report({
        cells: [cell({
          cost: { comparable: false, reported: 3, finished: 11,
                  reason: "not comparable: 3 of 11 attempts reported tokens" },
        })],
      }),
    );
    show();
    expect(await screen.findByTestId("harness-cost")).toHaveTextContent(
      "not comparable: 3 of 11 attempts reported tokens",
    );
    expect(screen.getByTestId("harness-cost")).not.toHaveTextContent("0 tokens");
  });

  it("marks a thin week in the series and leaves an unmeasured week absent", async () => {
    show();
    const series = await screen.findByTestId("harness-series");
    const points = within(series).getAllByTestId("harness-point");
    // Two recorded weeks, and no third invented for the gap between them.
    expect(points).toHaveLength(2);
    expect(points[0]).toHaveAttribute("data-below-floor", "true");
    expect(points[1]).toHaveAttribute("data-below-floor", "false");
  });

  it("offers version history and flags a cell that is not the current build", async () => {
    harness.mockResolvedValueOnce(
      report({
        versions: "all",
        cells: [cell({
          key: { ...cell().key, binary_version: "0.23.0" },
          versions_seen: ["0.23.0", "0.100.0"], is_current_version: false,
        })],
      }),
    );
    show();
    expect(await screen.findByTestId("harness-old-version")).toHaveTextContent(
      "not the current version",
    );
    expect(screen.getByTestId("harness-versions-seen")).toHaveTextContent("0.23.0, 0.100.0");
    expect(screen.getByLabelText("Binary versions")).toBeInTheDocument();
  });

  it("shows the platform average with the band it was served as, never an exact count", async () => {
    harness.mockResolvedValueOnce(
      report({
        platform: { cells_with_overlay: 1, min_orgs: 3, min_n: 20, max_org_share: 0.6 },
        platform_reason: "",
        cells: [cell({ platform: { rate: 0.71, n: "50–199", orgs: 4 } })],
      }),
    );
    show();
    const overlay = await screen.findByTestId("harness-platform");
    expect(overlay).toHaveTextContent("platform average 71% across 4 organisations (n 50–199)");
  });

  it("says why a cell has no platform average instead of leaving it blank", async () => {
    harness.mockResolvedValueOnce(
      report({
        cells: [cell({
          platform: { rate: null,
                      reason: "no platform average: fewer than three organisations contribute here" },
        })],
      }),
    );
    show();
    expect(await screen.findByTestId("harness-platform")).toHaveTextContent(
      "fewer than three organisations contribute here",
    );
  });

  it("says a self-hosted instance has no overlay and why", async () => {
    harness.mockResolvedValueOnce(
      report({
        platform: null,
        platform_reason:
          "no platform average on a self-hosted instance: it is built from other organisations' rollups, and there are none here",
      }),
    );
    show();
    expect(await screen.findByTestId("harness-no-platform")).toHaveTextContent(
      "self-hosted instance",
    );
  });

  it("breaks an org cell down by the projects behind it", async () => {
    harness.mockResolvedValueOnce(
      report({
        scope: "org", org_id: "org_1", projects: ["p_one", "p_two"],
        cells: [cell({ by_project: [
          { project_id: "p_one", finished: 6, signed_off: 6 },
          { project_id: "p_two", finished: 6, signed_off: 3 },
        ] })],
      }),
    );
    show();
    expect(await screen.findByTestId("harness-by-project")).toHaveTextContent(
      "p_one 6/6 · p_two 3/6",
    );
  });

  it("says nothing is measured rather than rendering an empty page", async () => {
    harness.mockResolvedValueOnce(report({ cells: [], below_floor_count: 0 }));
    show();
    expect(await screen.findByText(/Nothing measured yet/)).toBeInTheDocument();
  });
});
