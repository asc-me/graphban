import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { HarnessView } from "@/features/harness/HarnessView";
import { ProjectProvider } from "@/features/ProjectContext";
import type { HarnessCell, HarnessReport } from "@/lib/types";

function cell(over: Partial<HarnessCell> = {}): HarnessCell {
  return {
    key: {
      vendor: "gbagent", model: "qwen3.6", binary_version: "0.9.1",
      capability: "other", size_band: "M",
    },
    finished: 12, signed_off: 9, bounced: 3, rate: 0.75, below_floor: false,
    sampling: { first_choice: 6, fallback: 4, explicit: 2, unknown: 0, probe: 0 },
    kind: "other", family: "other", label: "other", leaves: [],
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
const probeCandidates = vi.fn(async () => ({
  project_id: "core",
  by_leaf: {},
  by_family: {},
  floor: 5,
  estimated_tokens: { comparable: false, reported: 0, finished: 0, reason: "no probe history reported tokens" },
  suggestions: [],
})) as ReturnType<typeof vi.fn>;
const startProbeRun = vi.fn(async () => ({ run_id: "run-1" }));

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [{ id: "core", name: "Core", tag: "GRPH" }]),
    harness: (...args: unknown[]) => harness(...(args as [])),
    harnessProbeCandidates: (...args: unknown[]) => probeCandidates(...(args as [])),
    startHarnessProbeRun: (...args: unknown[]) => startProbeRun(...(args as [])),
  },
}));

const fleetData = { profile: null, policy: null, measured: [] as Record<string, unknown>[] };
const useFleetMock = vi.fn(() => ({ data: fleetData, refetch: vi.fn() }));

vi.mock("@/lib/queries", async () => {
  const actual = await vi.importActual<typeof import("@/lib/queries")>("@/lib/queries");
  return {
    ...actual,
    useFleet: (...args: unknown[]) => useFleetMock(...(args as [])),
  };
});

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
          sampling: { first_choice: 9, fallback: 0, explicit: 1, unknown: 0, probe: 0 },
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

  it("greys an uninstalled row with the reason as the label, not as unmeasured", async () => {
    harness.mockResolvedValueOnce(
      report({
        unavailable: [{
          vendor: "claude", model: "sonnet", capability: "A4",
          reason: "not installed", label: "not installed",
          control: "gbfleet doctor", drops: 6, score: 0.9, layer: "platform",
        }],
      }),
    );
    show();
    expect(await screen.findByTestId("harness-unavailable")).toHaveTextContent("claude:sonnet");
    expect(screen.getByTestId("harness-unavailable-reason")).toHaveTextContent("not installed");
    expect(screen.getByText(/not because it is unmeasured/)).toBeInTheDocument();
  });

  it("says nothing is measured rather than rendering an empty page", async () => {
    harness.mockResolvedValueOnce(report({ cells: [], below_floor_count: 0 }));
    show();
    expect(await screen.findByText(/Nothing measured yet/)).toBeInTheDocument();
  });

  it("shows coverage and greys a leaf under a labelled family rollup", async () => {
    harness.mockResolvedValueOnce(
      report({
        coverage: { attempts: 10, with_leaf: 7, rate: 0.7 },
        cells: [cell({
          kind: "family", family: "B", label: "family rollup",
          key: { ...cell().key, capability: "B" },
          finished: 4, signed_off: 2, rate: 0.5, below_floor: true,
          leaves: [{
            ...cell(),
            kind: "leaf", family: "B", label: "B5",
            key: { ...cell().key, capability: "B5" },
            finished: 4, signed_off: 2, rate: 0.5, below_floor: true,
          }],
        })],
        below_floor_count: 1,
      }),
    );
    show();
    expect(await screen.findByTestId("harness-coverage")).toHaveTextContent(
      "Coverage 70% — 7/10 attempts tagged a leaf",
    );
    expect(screen.getByTestId("harness-family-label")).toHaveTextContent("family rollup");
    const leaf = screen.getByTestId("harness-leaf");
    expect(leaf).toHaveAttribute("data-below-floor", "true");
    expect(leaf).toHaveTextContent("B5");
    expect(leaf).toHaveTextContent("below the floor");
  });

  it("shows an other cell with its count rather than hiding it", async () => {
    harness.mockResolvedValueOnce(
      report({
        coverage: { attempts: 3, with_leaf: 0, rate: 0 },
        cells: [cell({ kind: "other", family: "other", label: "other", finished: 3 })],
      }),
    );
    show();
    expect(await screen.findByTestId("harness-other")).toHaveTextContent("other — 3");
  });

  it("shows natural and probe as two numbers, never a sum", async () => {
    harness.mockResolvedValueOnce(
      report({
        cells: [cell({
          finished: 4, signed_off: 4, rate: 1,
          samples: {
            natural: { n: 4, finished: 4, signed_off: 4, rate: 1, below_floor: true },
            probe: { n: 2, finished: 2, signed_off: 0, rate: 0, below_floor: true },
          },
        })],
      }),
    );
    show();
    const samples = await screen.findByTestId("harness-samples");
    expect(samples).toHaveTextContent("natural 4/4");
    expect(samples).toHaveTextContent("probe 0/2");
    expect(samples).not.toHaveTextContent("6");
  });

  it("shows utilization with reporting counts and build vs review cost", async () => {
    harness.mockResolvedValueOnce(
      report({
        cells: [cell({
          utilization: {
            tokens: { comparable: true, reported: 5, finished: 5, tokens_per_signed_off: 1000,
                      tokens_in: 5000, tokens_out: 0 },
            turns: { median: 4, budget_median: 10, reported: 5, finished: 5, reason: null },
            budget_hits: { hits: 1, reported: 5, share: 0.2, reason: null },
          },
          build_cost: { comparable: true, reported: 5, finished: 5, tokens_per_signed_off: 1000,
                        tokens_in: 5000, tokens_out: 0 },
          review_cost: { comparable: false, reported: 0, finished: 3,
                         reason: "not comparable: 0 of 3 reviews reported tokens" },
        })],
      }),
    );
    show();
    expect(await screen.findByTestId("harness-utilization")).toHaveTextContent("median 4 turns");
    expect(screen.getByTestId("harness-build-review-cost")).toHaveTextContent("cost of build");
    expect(screen.getByTestId("harness-build-review-cost")).toHaveTextContent("cost of review");
  });

  it("labels F cells by touchpoint overlap and greys them under five checks", async () => {
    harness.mockResolvedValueOnce(
      report({
        review_cells: [{
          kind: "review", family: "F", label: "by touchpoint overlap",
          key: { vendor: "anthropic", model: "sonnet", binary_version: "", capability: "A4", size_band: "S" },
          checked: 3, below_floor: true,
          f1: { rate: null, n: 0, false_bounce: 0, confirmed: 0 },
          f2: { rate: 0.3, n: 3, miss: 1, miss_unconfirmed: 1, confirmed: 1, label: "by touchpoint overlap" },
          f3: { unclassified: null, n: 0, other: 0 },
        }],
      }),
    );
    show();
    expect(await screen.findByTestId("harness-f2-label")).toHaveTextContent("by touchpoint overlap");
    expect(screen.getByTestId("harness-review-below-floor")).toHaveTextContent("3 of 5");
  });

  it("composes the profile and policy editors on the Performance tab with the grid", async () => {
    // D14: the grid and the profile/policy editor belong on the Performance tab.
    // The probe panel and recommendations are on the Changes & probes tab (PRD-47 S12).
    // Sabotage: drop the Preferences import — this fails.
    show();
    expect(await screen.findByTestId("harness-cell")).toBeInTheDocument();
    expect(screen.getByTestId("fleet-profile")).toBeInTheDocument();
    expect(screen.getByTestId("fleet-policy")).toBeInTheDocument();
    expect(screen.getByLabelText("Per-period token cap")).toBeInTheDocument();
    expect(screen.getByTestId("fleet-policy-period")).toBeInTheDocument();
  });
});

const probeItems = [
  { id: "item-1", key: "GRPH-1", title: "Fix auth bypass", capabilities: ["B5"], touchpoints: ["src/auth.ts"] },
  { id: "item-2", key: "GRPH-2", title: "Add retry logic", capabilities: ["B5"], touchpoints: ["src/api.ts"] },
  { id: "item-3", key: "GRPH-3", title: "Migrate schema", capabilities: ["B5"], touchpoints: ["src/db.ts"] },
  { id: "item-4", key: "GRPH-4", title: "Extra candidate", capabilities: ["B5"], touchpoints: ["src/extra.ts"] },
];

function probeData(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    project_id: "core",
    by_leaf: { B5: probeItems },
    by_family: {
      B: { leaf_ready: ["B5"], fallback: false, n: 4, items: probeItems, thin_leaves: [] },
    },
    floor: 5,
    estimated_tokens: { comparable: true, reported: 3, finished: 5, tokens_per_attempt: 12000, tokens_in: 30000, tokens_out: 6000 },
    suggestions: [
      { trigger: "new_row", vendor: "anthropic", model: "sonnet", binary_version: "", reason: "no natural cell" },
    ],
    ...over,
  };
}

async function showChangesTab() {
  show();
  const user = userEvent.setup();
  await screen.findByTestId("harness-view");
  await user.click(screen.getByTestId("harness-tab-changes"));
  return user;
}

describe("Harness probe panel", () => {
  it("shows the estimated token cost before start", async () => {
    probeCandidates.mockResolvedValueOnce(probeData());
    await showChangesTab();
    const estimate = await screen.findByTestId("harness-probe-estimate");
    expect(estimate).toHaveTextContent("12,000 tokens per attempt");
    expect(estimate).toHaveTextContent("3 reported");
  });

  it("renders candidates grouped by leaf with checkboxes", async () => {
    probeCandidates.mockResolvedValueOnce(probeData());
    await showChangesTab();
    const group = await screen.findByTestId("harness-probe-group");
    expect(group).toHaveTextContent("B5");
    expect(group).toHaveTextContent("4 candidates");
    const items = within(group).getAllByTestId("harness-probe-item");
    expect(items).toHaveLength(4);
  });

  it("limits picks to three items across all groups", async () => {
    probeCandidates.mockResolvedValueOnce(probeData());
    await showChangesTab();
    await screen.findByTestId("harness-probe-panel");
    const checkboxes = screen.getAllByRole("checkbox");
    const user = userEvent.setup();
    await user.click(checkboxes[0]);
    await user.click(checkboxes[1]);
    await user.click(checkboxes[2]);
    expect(checkboxes[3]).toBeDisabled();
  });

  it("shows the start button only when a suggestion and items are picked", async () => {
    probeCandidates.mockResolvedValueOnce(probeData());
    harness.mockResolvedValueOnce(report({
      probe_suggestions: [{ trigger: "new_row", vendor: "anthropic", model: "sonnet", binary_version: "", estimated_tokens: { comparable: false, reported: 0, finished: 0, reason: "no history" }, reason: "no natural cell" }],
    }));
    const user = await showChangesTab();
    await screen.findByTestId("harness-probe-panel");
    expect(screen.queryByTestId("harness-probe-start")).not.toBeInTheDocument();
    const suggestionBtns = screen.getAllByTestId("harness-probe-suggestion");
    await user.click(suggestionBtns[0]);
    expect(screen.queryByTestId("harness-probe-start")).not.toBeInTheDocument();
    const checkboxes = screen.getAllByRole("checkbox");
    await user.click(checkboxes[0]);
    expect(await screen.findByTestId("harness-probe-start")).toHaveTextContent("Start probe");
    expect(screen.getByTestId("harness-probe-start")).toHaveTextContent("anthropic:sonnet");
  });

  it("calls startHarnessProbeRun with the picked items and suggestion", async () => {
    probeCandidates.mockResolvedValueOnce(probeData());
    harness.mockResolvedValueOnce(report({
      probe_suggestions: [{ trigger: "new_row", vendor: "anthropic", model: "sonnet", binary_version: "", estimated_tokens: { comparable: false, reported: 0, finished: 0, reason: "no history" }, reason: "no natural cell" }],
    }));
    const user = await showChangesTab();
    await screen.findByTestId("harness-probe-panel");
    await user.click(screen.getAllByTestId("harness-probe-suggestion")[0]);
    const checkboxes = screen.getAllByRole("checkbox");
    await user.click(checkboxes[0]);
    await user.click(checkboxes[1]);
    await user.click(await screen.findByTestId("harness-probe-start"));
    expect(startProbeRun).toHaveBeenCalledWith({
      project_id: "core",
      vendor: "anthropic",
      model: "sonnet",
      capability: "B5",
      item_ids: ["item-1", "item-2"],
      trigger: "new_row",
      binary_version: "",
    });
  });

  it("shows a 409 as a visible refusal, not a silent no-op", async () => {
    startProbeRun.mockRejectedValueOnce(new Error("a probe for this model is already running; one model and one leaf at a time"));
    probeCandidates.mockResolvedValueOnce(probeData());
    harness.mockResolvedValueOnce(report({
      probe_suggestions: [{ trigger: "new_row", vendor: "anthropic", model: "sonnet", binary_version: "", estimated_tokens: { comparable: false, reported: 0, finished: 0, reason: "no history" }, reason: "no natural cell" }],
    }));
    const user = await showChangesTab();
    await screen.findByTestId("harness-probe-panel");
    await user.click(screen.getAllByTestId("harness-probe-suggestion")[0]);
    await user.click(screen.getAllByRole("checkbox")[0]);
    await user.click(await screen.findByTestId("harness-probe-start"));
    const err = await screen.findByTestId("harness-probe-error");
    expect(err).toHaveTextContent("already running");
  });

  it("renders nothing when there are no candidates and no suggestions", async () => {
    probeCandidates.mockResolvedValueOnce(probeData({ by_leaf: {}, by_family: {}, suggestions: [] }));
    harness.mockResolvedValueOnce(report({ probe_suggestions: [] }));
    await showChangesTab();
    expect(screen.queryByTestId("harness-probe-panel")).not.toBeInTheDocument();
  });

  it("groups at family level when all leaves are below the floor", async () => {
    probeCandidates.mockResolvedValueOnce(probeData({
      by_family: {
        B: { leaf_ready: ["B5"], fallback: true, n: 4, items: probeItems, thin_leaves: ["B5"] },
      },
    }));
    await showChangesTab();
    const group = await screen.findByTestId("harness-probe-group");
    expect(group).toHaveTextContent("family");
  });
});

describe("Harness tabs (PRD-47 S12)", () => {
  it("renders three tabs with Performance active by default", async () => {
    show();
    await screen.findByTestId("harness-view");
    expect(screen.getByTestId("harness-tab-performance")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("harness-tab-guidance")).toHaveAttribute("aria-selected", "false");
    expect(screen.getByTestId("harness-tab-changes")).toHaveAttribute("aria-selected", "false");
  });

  it("switches to the Guidance tab and shows the generation stamp", async () => {
    harness.mockResolvedValueOnce(report({
      grading_rules: [
        { rule: "R1", label: "promote", thresholds: { min_finished: 10, min_rate: 0.8 } },
      ],
    }));
    show();
    const user = userEvent.setup();
    await screen.findByTestId("harness-view");
    await user.click(screen.getByTestId("harness-tab-guidance"));
    expect(await screen.findByTestId("guidance-generation-stamp")).toBeInTheDocument();
    expect(screen.getByTestId("guidance-generation-stamp")).toHaveTextContent("window 90 days");
  });

  it("shows the effort curve on the Performance tab for above-floor non-family cells", async () => {
    harness.mockResolvedValueOnce(report({
      cells: [
        cell({ key: { ...cell().key, size_band: "S" }, finished: 10, signed_off: 8, rate: 0.8, below_floor: false }),
        cell({ key: { ...cell().key, size_band: "L" }, finished: 6, signed_off: 3, rate: 0.5, below_floor: false }),
        cell({ key: { ...cell().key, size_band: "M" }, finished: 3, signed_off: 2, rate: 0.667, below_floor: true, kind: "leaf" }),
      ],
    }));
    show();
    const curve = await screen.findByTestId("harness-effort-curve");
    expect(curve).toBeInTheDocument();
    expect(screen.getByTestId("harness-effort-band-S")).toHaveTextContent("80%");
    expect(screen.getByTestId("harness-effort-band-L")).toHaveTextContent("50%");
    // Below-floor cell excluded from the curve
    expect(screen.queryByTestId("harness-effort-band-M")).not.toBeInTheDocument();
  });

  it("shows recommendations on the Changes tab with the 'this page changes nothing' framing", async () => {
    show();
    const user = userEvent.setup();
    await screen.findByTestId("harness-view");
    await user.click(screen.getByTestId("harness-tab-changes"));
    expect(await screen.findByText(/this page changes nothing/i)).toBeInTheDocument();
  });
});

describe("Guidance tab — served from the backend (GRPH-963)", () => {
  async function showGuidanceTab(over: Partial<HarnessReport> = {}) {
    harness.mockResolvedValueOnce(report({
      grading_rules: [
        { rule: "R1", label: "promote", thresholds: { min_finished: 10, min_rate: 0.8 } },
        { rule: "R2", label: "demote", thresholds: { min_finished: 6, max_rate: 0.25 } },
        { rule: "R3", label: "reweight", thresholds: { min_n: 8, margin: 0.3, min_families: 2 } },
        { rule: "R4", label: "policy", thresholds: { min_n: 6, bounce_rate: 0.7, keep_rate: 0.7 } },
        { rule: "R5", label: "reprior", thresholds: { min_n: 5, margin: 0.3 } },
        { rule: "R6", label: "install", thresholds: { min_drops: 6, margin: 0.3 } },
      ],
      ...over,
    }));
    show();
    const user = userEvent.setup();
    await screen.findByTestId("harness-view");
    await user.click(screen.getByTestId("harness-tab-guidance"));
    return user;
  }

  it("renders grading rules from the fetched payload, not hardcoded", async () => {
    await showGuidanceTab();
    const rules = await screen.findAllByTestId("guidance-rule");
    expect(rules).toHaveLength(6);
    // R2 from the payload: min_finished 6, max_rate 0.25
    const r2 = screen.getByTestId("guidance-rule");
    const r2El = rules.find((r) => r.getAttribute("data-rule") === "R2");
    expect(r2El).toHaveTextContent("min_finished 6");
    expect(r2El).toHaveTextContent("max_rate 0.25");
  });

  it("shows the routing table from fleet_status.measured", async () => {
    useFleetMock.mockReturnValueOnce({
      data: {
        profile: null, policy: null,
        measured: [
          { vendor: "gbagent", model: "qwen3.6", capability: "B5", layer: "project",
            quality: { value: 0.75, n: 12 },
            latency: { value: 0.8, n: 10, median_seconds: 620 },
            bands: { S: { value: 0.8, n: 6 }, M: { value: 0.7, n: 6 } } },
        ],
      },
      refetch: vi.fn(),
    });
    await showGuidanceTab();
    const rows = await screen.findAllByTestId("guidance-measured-row");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("gbagent:qwen3.6");
    expect(rows[0]).toHaveTextContent("B5");
    expect(rows[0]).toHaveTextContent("75%");
  });

  it("shows 'As served' with the raw fleet_status JSON", async () => {
    await showGuidanceTab();
    const asServed = await screen.findByTestId("guidance-as-served");
    expect(asServed).toBeInTheDocument();
    const json = screen.getByTestId("guidance-as-served-json");
    expect(json).toHaveTextContent("profile");
  });

  // CALL sabotage: if the grading rules were hardcoded instead of served from the payload,
  // this test would pass with the wrong values. It mocks R2 with max_rate 0.25 and asserts
  // the rendered value matches — a hardcoded 0.5 would fail.
  it("CALL sabotage: rendered thresholds match the payload, not a hardcoded copy", async () => {
    await showGuidanceTab({
      grading_rules: [
        { rule: "R2", label: "demote", thresholds: { min_finished: 6, max_rate: 0.25 } },
      ],
    });
    const rules = await screen.findAllByTestId("guidance-rule");
    expect(rules).toHaveLength(1);
    expect(rules[0]).toHaveTextContent("max_rate 0.25");
    expect(rules[0]).not.toHaveTextContent("max_rate 0.5");
  });
});
