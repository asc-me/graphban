/**
 * GRPH-1003 / PRD-47 G3 — the editable tier map: which model each harness runs when.
 *
 * The read-only catalog table (GRPH-866) says what CAN run and stayed exactly as it was.
 * These cover the control the design draws and that table was missing.
 *
 * Three assertions here carry the weight, because each is a defect class this repo has
 * already shipped:
 *
 *   • Clear overrides falls back to the PACKAGED model, never to a blank cell. An absent
 *     override reading as an absent model is `absence-reads-as-clean` wearing a dropdown.
 *   • Inherit fills only MEASURED cells. A cell with `graded_model: null` is left alone and
 *     named "not measured" — a value invented there would be an override nobody chose,
 *     carrying the authority of a measurement that does not exist.
 *   • A failed read renders as a failure and a failed save renders as a failure. Neither may
 *     fall through to "no harness to map", which is the reassuring reading of both.
 *
 * The panel is rendered directly here; `fleet-v2.test.tsx` asserts the CALL — that FleetV2View
 * actually mounts it — because a correct panel nobody renders is the same gap from the
 * other side.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TierMapPanel } from "@/features/fleet/matrixTable";
import type { FleetTierCell, FleetTierMap } from "@/lib/types";

const api = vi.hoisted(() => ({
  fleetTierMap: vi.fn(),
  saveFleetTierMap: vi.fn(),
  clearFleetTierMap: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ setActiveProjectId: vi.fn(), api }));

function cell(over: Partial<FleetTierCell> = {}): FleetTierCell {
  return {
    harness: "gbagent", tier: "cheap", packaged_model: "qwen3.6",
    override: null, effective_model: "qwen3.6", overridden: false,
    models: ["qwen3.6", "haiku"], graded_model: "haiku",
    ...over,
  };
}

function map(cells: FleetTierCell[], over: Partial<FleetTierMap> = {}): FleetTierMap {
  return { rows: [], cells, overridden: cells.some((c) => c.override !== null), ...over };
}

/** The accessible name of the gbagent/cheap dropdown — a select per harness × tier. */
const CHEAP = "Model for gbagent at cheap";
const CODEX = "Model for codex at frontier";

function show() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/fleet.v2"]}>
        <TierMapPanel projectId="core" harnessHref="/harness" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, qc };
}

/**
 * Wait for the cells, not for the panel.
 *
 * The heading and both links render during the skeleton and during a failed read as well, so
 * `findByRole("heading")` proves nothing about the fetch — it passed while the grid was still
 * pulsing. The status line exists only once cells have arrived.
 */
async function loaded() {
  await screen.findByTestId("tier-map-status");
}

describe("tier map panel", () => {
  beforeEach(() => {
    api.fleetTierMap.mockReset();
    api.saveFleetTierMap.mockReset();
    api.clearFleetTierMap.mockReset();
  });

  it("reads the map for the active project and offers every control", async () => {
    api.fleetTierMap.mockResolvedValue(map([cell()]));
    show();
    await loaded();
    expect(screen.getByRole("heading", { name: "Tier map" })).toBeInTheDocument();
    expect(screen.getByText("Which model each harness runs when")).toBeInTheDocument();
    expect(api.fleetTierMap).toHaveBeenCalledWith("core");

    expect(screen.getByRole("button", { name: /Inherit from performance grading/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Clear overrides/ })).toBeInTheDocument();
    // Nothing has been edited, so there is nothing to write.
    expect(screen.getByRole("button", { name: /Save tier map/ })).toBeDisabled();
    expect(screen.getByTestId("tier-map-grading-link")).toHaveAttribute("href", "/harness");
    expect(screen.getByTestId("tier-map-performance-link")).toHaveAttribute("href", "/harness");

    // The tier column is named by the server's own tier, and the select's resting value is
    // "no override" — the packaged model is an OPTION, never a stored choice.
    expect(screen.getByRole("columnheader", { name: "Tier · cheap" })).toBeInTheDocument();
    const select = screen.getByLabelText(CHEAP);
    expect(select).toHaveValue("");
    expect(within(select).getByRole("option", { name: /packaged/ })).toHaveTextContent("qwen3.6");
    expect(within(select).getAllByRole("option").map((o) => o.textContent?.trim()))
      .toEqual(["packaged · qwen3.6", "qwen3.6", "haiku"]);
    expect(screen.getByTestId("tier-map-effective")).toHaveTextContent("qwen3.6");
    expect(screen.getByTestId("tier-map-status")).toHaveTextContent(
      "No overrides saved — every cell runs its packaged model.");
  });

  it("derives the tier columns from the cells, so a third tier appears", async () => {
    api.fleetTierMap.mockResolvedValue(map([
      cell(),
      cell({ tier: "frontier", packaged_model: "opus", effective_model: "opus",
             models: ["opus"], graded_model: null }),
      cell({ harness: "codex", tier: "mid", packaged_model: "gpt-5", effective_model: "gpt-5",
             models: ["gpt-5"], graded_model: null }),
    ]));
    show();
    await loaded();
    expect(screen.getByRole("columnheader", { name: "Tier · cheap" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Tier · frontier" })).toBeInTheDocument();
    // Not a hardcoded cheap/frontier pair: the tier the catalog named is the column drawn.
    expect(screen.getByRole("columnheader", { name: "Tier · mid" })).toBeInTheDocument();
    expect(screen.getByLabelText("Model for codex at mid")).toBeInTheDocument();
    // Squares the matrix does not cover are named rather than left blank — gbagent has no
    // `mid` and codex has neither `cheap` nor `frontier`.
    expect(screen.getAllByText("not in catalog")).toHaveLength(3);
  });

  it("an override changes what will run and enables Save", async () => {
    api.fleetTierMap.mockResolvedValue(map([cell()]));
    api.saveFleetTierMap.mockResolvedValue(
      map([cell({ override: "haiku", effective_model: "haiku", overridden: true })]));
    const user = userEvent.setup();
    show();
    const save = await screen.findByRole("button", { name: /Save tier map/ });

    await user.selectOptions(screen.getByLabelText(CHEAP), "haiku");

    expect(screen.getByTestId("tier-map-effective")).toHaveTextContent("haiku");
    // The overridden cell is marked, and marked as not yet written.
    expect(screen.getByText("override")).toBeInTheDocument();
    expect(screen.getByText("unsaved")).toBeInTheDocument();
    expect(screen.getByTestId("tier-map-status")).toHaveTextContent("Unsaved changes in 1 cell.");
    expect(save).toBeEnabled();

    await user.click(save);
    await waitFor(() => expect(api.saveFleetTierMap).toHaveBeenCalledTimes(1));
    expect(api.saveFleetTierMap).toHaveBeenCalledWith({
      project_id: "core",
      cells: [{ harness: "gbagent", tier: "cheap", model: "haiku" }],
    });
    // What was just saved becomes the baseline, so the same draft is no longer pending and
    // the cell keeps its override marker.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Save tier map/ })).toBeDisabled());
    expect(screen.getByText("override")).toBeInTheDocument();
    expect(screen.queryByText("unsaved")).not.toBeInTheDocument();
    expect(screen.getByTestId("tier-map-status")).toHaveTextContent("1 saved override");
  });

  it("putting a cell back on packaged writes model:null, not the packaged name", async () => {
    api.fleetTierMap.mockResolvedValue(
      map([cell({ override: "haiku", effective_model: "haiku", overridden: true })]));
    api.saveFleetTierMap.mockResolvedValue(map([cell()]));
    const user = userEvent.setup();
    show();
    await loaded();
    expect(screen.getByTestId("tier-map-effective")).toHaveTextContent("haiku");
    expect(screen.getByTestId("tier-map-status")).toHaveTextContent("1 saved override · nothing unsaved.");

    await user.selectOptions(screen.getByLabelText(CHEAP), "");

    expect(screen.getByTestId("tier-map-effective")).toHaveTextContent("qwen3.6");
    expect(screen.queryByText("override")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Save tier map/ }));
    await waitFor(() => expect(api.saveFleetTierMap).toHaveBeenCalledWith({
      project_id: "core",
      cells: [{ harness: "gbagent", tier: "cheap", model: null }],
    }));
  });

  it("Inherit fills the measured cells only, and names the one that is not measured", async () => {
    api.fleetTierMap.mockResolvedValue(map([
      cell(),
      cell({ harness: "codex", tier: "frontier", packaged_model: "gpt-5", effective_model: "gpt-5",
             models: ["gpt-5", "o3"], graded_model: null }),
    ]));
    const user = userEvent.setup();
    show();
    await loaded();
    // Captured before the click: the panel re-renders in place, so this is still the square.
    const codexSquare = screen.getByLabelText(CODEX).closest("td") as HTMLElement;

    await user.click(screen.getByRole("button", { name: /Inherit from performance grading/ }));

    // gbagent was graded to haiku, so haiku is what it will run.
    expect(screen.getByLabelText(CHEAP)).toHaveValue("haiku");
    // codex has no measurement: the draft is untouched and the square says why.
    expect(screen.getByLabelText(CODEX)).toHaveValue("");
    expect(within(codexSquare).getByTestId("tier-map-effective")).toHaveTextContent("gpt-5");
    expect(within(codexSquare).getByTestId("tier-map-graded")).toHaveTextContent("not measured");
    expect(screen.getByTestId("tier-map-note")).toHaveTextContent(/1 not measured/);
    // Only the measured cell became a pending change — nothing was invented for the other.
    expect(screen.getByTestId("tier-map-status")).toHaveTextContent("Unsaved changes in 1 cell.");
  });

  it("Inherit with nothing measured changes no cell at all", async () => {
    api.fleetTierMap.mockResolvedValue(map([cell({ graded_model: null })]));
    const user = userEvent.setup();
    show();
    await loaded();
    await user.click(screen.getByRole("button", { name: /Inherit from performance grading/ }));
    expect(screen.getByTestId("tier-map-note")).toHaveTextContent(/no cell in this catalog is measured yet/);
    expect(screen.getByLabelText(CHEAP)).toHaveValue("");
    expect(screen.getByTestId("tier-map-effective")).toHaveTextContent("qwen3.6");
    expect(screen.getByRole("button", { name: /Save tier map/ })).toBeDisabled();
  });

  /**
   * The defect this panel is most likely to grow. "Clear" that blanks the cell instead of
   * restoring the committed one turns a config reset into a harness that resolves to nothing,
   * and on screen it looks like a tidy empty dropdown rather than a broken matrix.
   */
  it("Clear overrides falls back to the PACKAGED model, never to a blank cell", async () => {
    api.fleetTierMap.mockResolvedValue(
      map([cell({ override: "haiku", effective_model: "haiku", overridden: true })]));
    // What DELETE answers with: the same catalog, every override gone, effective back to the
    // committed model.
    api.clearFleetTierMap.mockResolvedValue(map([cell()]));
    const user = userEvent.setup();
    show();
    await loaded();
    expect(screen.getByTestId("tier-map-effective")).toHaveTextContent("haiku");

    await user.click(screen.getByRole("button", { name: /Clear overrides/ }));

    await waitFor(() => expect(api.clearFleetTierMap).toHaveBeenCalledTimes(1));
    expect(api.clearFleetTierMap).toHaveBeenCalledWith("core");
    expect(screen.getByTestId("tier-map-effective")).toHaveTextContent("qwen3.6");
    expect(screen.getByTestId("tier-map-effective")).not.toHaveTextContent("—");
    expect(screen.getByLabelText(CHEAP)).toHaveValue("");
    expect(within(screen.getByLabelText(CHEAP)).getByRole("option", { name: /packaged/ }))
      .toHaveTextContent("qwen3.6");
    expect(screen.queryByText("override")).not.toBeInTheDocument();
    expect(screen.getByTestId("tier-map-status")).toHaveTextContent(
      "No overrides saved — every cell runs its packaged model.");
  });

  it("Clear overrides also discards an UNSAVED draft, which is what the button is for", async () => {
    // Nothing is saved to clear, so DELETE answers with the very map it was handed — the
    // baseline does not move, and the draft would survive without an explicit reset.
    const served = map([cell()]);
    api.fleetTierMap.mockResolvedValue(served);
    api.clearFleetTierMap.mockResolvedValue(map([cell()]));
    const user = userEvent.setup();
    show();
    await loaded();
    await user.selectOptions(screen.getByLabelText(CHEAP), "haiku");
    expect(screen.getByTestId("tier-map-effective")).toHaveTextContent("haiku");

    await user.click(screen.getByRole("button", { name: /Clear overrides/ }));

    await waitFor(() => expect(api.clearFleetTierMap).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("tier-map-effective")).toHaveTextContent("qwen3.6");
    expect(screen.getByRole("button", { name: /Save tier map/ })).toBeDisabled();
  });

  it("a cell with nothing committed says so instead of rendering a blank model", async () => {
    api.fleetTierMap.mockResolvedValue(map([cell({ packaged_model: "", effective_model: "", models: [] })]));
    show();
    await loaded();
    expect(screen.getByTestId("tier-map-effective")).toHaveTextContent("—");
    expect(screen.getByRole("option", { name: /packaged/ })).toHaveTextContent("nothing committed");
    // One option, and it is the sentinel: no empty option that reads as a real choice.
    expect(screen.getAllByRole("option")).toHaveLength(1);
  });

  /**
   * Declared value ≠ rendered value. A saved override the catalog no longer names has no
   * matching `<option>`, and a select whose value matches nothing paints its FIRST option —
   * so the dropdown would read "packaged · qwen3.6" while the line under it said the override,
   * with no way to see or undo the value on screen.
   */
  it("keeps a value the picklist does not name selectable and labelled as such", async () => {
    api.fleetTierMap.mockResolvedValue(map([
      cell({ override: "sonnet-3.5", effective_model: "sonnet-3.5", overridden: true,
             models: ["qwen3.6", "haiku"] }),
    ]));
    show();
    const select = await screen.findByLabelText(CHEAP);
    expect(select).toHaveValue("sonnet-3.5");
    expect(within(select).getByRole("option", { name: /sonnet-3\.5/ }))
      .toHaveTextContent("not in this catalog");
    expect(screen.getByTestId("tier-map-effective")).toHaveTextContent("sonnet-3.5");
  });

  it("surfaces a failed save rather than swallowing it", async () => {
    api.fleetTierMap.mockResolvedValue(map([cell()]));
    api.saveFleetTierMap.mockRejectedValue(
      new Error('{"detail":"tier map rejected: unknown model"}'));
    const user = userEvent.setup();
    show();
    await loaded();
    await user.selectOptions(screen.getByLabelText(CHEAP), "haiku");
    await user.click(screen.getByRole("button", { name: /Save tier map/ }));

    const surface = await screen.findByTestId("tier-map-save-error");
    // The server's own reason, not a raw JSON envelope and not a generic "something failed".
    expect(surface).toHaveTextContent("tier map rejected: unknown model");
    expect(within(surface).getByRole("alert")).toBeInTheDocument();
    expect(within(surface).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    // A failed write must not look like a save: the edit is still pending and still editable.
    expect(screen.getByTestId("tier-map-effective")).toHaveTextContent("haiku");
    expect(screen.getByTestId("tier-map-status")).toHaveTextContent("Unsaved changes in 1 cell.");
    expect(screen.getByRole("button", { name: /Save tier map/ })).toBeEnabled();
    expect(screen.queryByTestId("tier-map-empty")).not.toBeInTheDocument();
  });

  it("surfaces a failed clear, and keeps the overrides it could not remove", async () => {
    api.fleetTierMap.mockResolvedValue(
      map([cell({ override: "haiku", effective_model: "haiku", overridden: true })]));
    api.clearFleetTierMap.mockRejectedValue(
      new Error('{"detail":"tier map is read-only on this deployment"}'));
    const user = userEvent.setup();
    show();
    await loaded();
    await user.click(screen.getByRole("button", { name: /Clear overrides/ }));

    const surface = await screen.findByTestId("tier-map-clear-error");
    expect(surface).toHaveTextContent("tier map is read-only on this deployment");
    expect(within(surface).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    // Claiming the overrides are gone when the DELETE failed is the worse half of this bug.
    expect(screen.getByTestId("tier-map-effective")).toHaveTextContent("haiku");
    expect(screen.getByText("override")).toBeInTheDocument();
  });

  it("a failed READ is an error, not an empty catalog", async () => {
    api.fleetTierMap.mockRejectedValue(new Error("boom"));
    show();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The request failed — nothing here means empty.");
    expect(alert).toHaveTextContent('this is not "nothing overridden"');
    // Both halves: the failure is named AND the reassuring copy is absent.
    expect(screen.queryByTestId("tier-map-empty")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Save tier map/ })).not.toBeInTheDocument();
    // The panel is still on the page, so its absence is never mistaken for a missing feature.
    expect(screen.getByRole("heading", { name: "Tier map" })).toBeInTheDocument();
  });

  it("says the catalog has not been served, verbatim, when there are no cells", async () => {
    api.fleetTierMap.mockResolvedValue(map([]));
    show();
    const empty = await screen.findByTestId("tier-map-empty");
    expect(empty).toHaveTextContent("No harness to map until the catalog is served.");
    // Empty and failed are different states and must not render alike.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Save tier map/ })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Tier map" })).toBeInTheDocument();
  });

  it("keeps an unsaved edit when the map is re-read and nothing was saved", async () => {
    // A fresh object every read, same values: an effect or a key pinned to the RESPONSE would
    // remount the form and throw the edit away, which is what `LogExportPanel` documents.
    api.fleetTierMap.mockImplementation(async () => map([cell()]));
    const user = userEvent.setup();
    const { qc } = show();
    await loaded();
    await user.selectOptions(screen.getByLabelText(CHEAP), "haiku");
    expect(screen.getByTestId("tier-map-status")).toHaveTextContent("Unsaved changes in 1 cell.");

    // Also pins the AGENTS.md invariant: the query key carries the project id.
    await qc.invalidateQueries({ queryKey: ["fleet-tier-map", "core"] });
    await waitFor(() => expect(api.fleetTierMap).toHaveBeenCalledTimes(2));

    expect(screen.getByTestId("tier-map-status")).toHaveTextContent("Unsaved changes in 1 cell.");
    expect(screen.getByTestId("tier-map-effective")).toHaveTextContent("haiku");
  });
});
