import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { Recommendations } from "@/features/harness/Recommendations";
import { ProjectProvider } from "@/features/ProjectContext";
import type { HarnessCard, HarnessRecommendations } from "@/lib/types";

function key(over = {}) {
  return {
    vendor: "gbagent", model: "qwen3.6", binary_version: "1.0.0",
    capability: "other", size_band: "L", ...over,
  };
}

function card(over: Partial<HarnessCard> = {}): HarnessCard {
  return {
    rule: "R1", key: "R1:gbagent:qwen3.6:1.0.0", evidence_hash: "abc123",
    title: "promote gbagent:qwen3.6 for backend",
    detail: "signed off 10 of 10 in backend/general/L and its matrix row is still unverified.",
    cells: [{ cell: key(), finished: 10, signed_off: 10, rate: 1 }],
    siblings: [{ cell: key({ size_band: "S" }), finished: 9, signed_off: 2, rate: 0.222 }],
    draft: { target: "gbagent:qwen3.6", where: "fleet/src/gbfleet/matrix.toml",
             evidence_line: "verified: 10/10 signed off in backend/general/L" },
    replay: { considered: 19, changed: 4, skipped_no_resolution: 2, truncated: false,
              moves: [{ from: "claude:sonnet", to: "gbagent:qwen3.6", count: 4 }],
              summary: "would have changed 4 of 19 recorded resolutions; 2 had no recorded resolution to replay" },
    thresholds: { min_finished: 10, min_rate: 0.8 },
    state: "new", previously: null, ...over,
  };
}

function payload(over: Partial<HarnessRecommendations> = {}): HarnessRecommendations {
  return { project_id: "core", cards: [card()], rules: ["R1", "R2", "R3", "R4"],
           lessons_drafted: [], window_days: 90, floor: 5, ...over };
}

const cards = vi.fn(async () => payload());
const mark = vi.fn(async () => ({ card_key: "R1:gbagent:qwen3.6:1.0.0", state: "accepted",
                                  evidence_hash: "abc123", lesson_drafted: "m_1" }));

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [{ id: "core", name: "Core", tag: "GRPH" }]),
    harnessRecommendations: (...a: unknown[]) => cards(...(a as [])),
    markHarnessRecommendation: (...a: unknown[]) => mark(...(a as [])),
  },
}));

function show() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/harness"]}>
        <ProjectProvider>
          <Recommendations projectId="core" />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Harness recommendations", () => {
  it("still renders a probe card whose replay has no moves (live payload)", async () => {
    cards.mockResolvedValueOnce(payload({
      cards: [card({
        rule: "probe",
        key: "probe:anthropic:claude-opus-5:",
        title: "Probe anthropic:claude-opus-5",
        detail: "a harness first resolved with no cell for it",
        cells: [],
        siblings: [],
        draft: { target: "anthropic:claude-opus-5", trigger: "new_row", scheduled: false },
        replay: {} as HarnessCard["replay"],
        thresholds: { floor: 5 },
      })],
    }));
    show();
    expect(await screen.findByTestId("harness-card")).toHaveAttribute("data-rule", "probe");
    expect(screen.getByText("Probe anthropic:claude-opus-5")).toBeInTheDocument();
  });

  it("names the rule and shows the replay rather than a bare suggestion", async () => {
    show();
    const row = await screen.findByTestId("harness-card");
    expect(row).toHaveAttribute("data-rule", "R1");
    expect(screen.getByTestId("harness-card-replay")).toHaveTextContent(
      "would have changed 4 of 19 recorded resolutions",
    );
    // The replay must not hide what it could not speak for.
    expect(screen.getByTestId("harness-card-replay")).toHaveTextContent(
      "2 had no recorded resolution",
    );
  });

  it("hides the replay line when it moves nothing, keeps it when it does", async () => {
    cards.mockResolvedValueOnce(payload({
      cards: [card({
        replay: { considered: 66, changed: 0, skipped_no_resolution: 0, truncated: false,
                  moves: [], summary: "would have changed 0 of 66 recorded resolutions" },
      })],
    }));
    show();
    await screen.findByTestId("harness-card");
    expect(screen.queryByTestId("harness-card-replay")).not.toBeInTheDocument();
  });

  it("shows the sibling cells it did not fire on, and says that is what accepting generalises over", async () => {
    show();
    fireEvent.click(await screen.findByTestId("harness-card-expand"));
    const siblings = screen.getByTestId("harness-card-siblings");
    expect(siblings).toHaveTextContent("generalises over");
    expect(within(siblings).getByText(/other\/S · 2\/9/)).toBeInTheDocument();
  });

  it("says the page applies nothing and points at where the change is made", async () => {
    show();
    fireEvent.click(await screen.findByTestId("harness-card-expand"));
    const apply = screen.getByTestId("harness-card-apply");
    expect(apply).toHaveTextContent("this page changes nothing");
    expect(apply).toHaveTextContent("fleet/src/gbfleet/matrix.toml");
    expect(apply).toHaveTextContent("verified: 10/10 signed off in backend/general/L");
  });

  it("marks a card with its evidence hash so the mark expires when the numbers move", async () => {
    show();
    fireEvent.click(await screen.findByTestId("harness-accept"));
    await waitFor(() =>
      expect(mark).toHaveBeenCalledWith({
        project_id: "core", card_key: "R1:gbagent:qwen3.6:1.0.0",
        evidence_hash: "abc123", action: "accept",
      }),
    );
  });

  it("tells a person when a card they already acted on has come back", async () => {
    cards.mockResolvedValueOnce(payload({
      cards: [card({ previously: { state: "accepted", at: "2026-09-01T00:00:00Z",
                                   evidence_changed: true } })],
    }));
    show();
    expect(await screen.findByTestId("harness-card-returned")).toHaveTextContent(
      "the evidence has changed since",
    );
  });

  it("says why there is nothing to recommend instead of rendering blank", async () => {
    cards.mockResolvedValueOnce(payload({ cards: [] }));
    show();
    expect(await screen.findByTestId("harness-no-cards")).toHaveTextContent(
      "R1–R7 fire on cells above the 5-attempt floor",
    );
  });

  describe("filters", () => {
    const mk = (rule: string, n: number, over: Partial<HarnessCard> = {}) =>
      card({ rule, key: `${rule}:${n}`, ...over });
    const fixture = () =>
      payload({ cards: [mk("R2", 1), mk("R5", 1), mk("R5", 2), mk("R5", 3), mk("R7", 1)] });

    it("defaults to every rule but R5, and still counts R5", async () => {
      window.localStorage.clear();
      cards.mockResolvedValueOnce(fixture());
      show();
      await screen.findByTestId("harness-cards");
      expect(screen.getAllByTestId("harness-card")).toHaveLength(2);
      const r5 = screen.getAllByTestId("harness-rule-chip").find((c) => c.dataset.rule === "R5")!;
      expect(within(r5).getByTestId("harness-rule-count")).toHaveTextContent("3");
      fireEvent.click(r5);
      expect(screen.getAllByTestId("harness-card")).toHaveLength(5);
    });

    it("narrows by harness and by state", async () => {
      window.localStorage.clear();
      cards.mockResolvedValueOnce(payload({ cards: [
        mk("R2", 1, { cells: [{ cell: key({ vendor: "anthropic", model: "opus" }), finished: 1, signed_off: 1, rate: 1 }] }),
        // Server-shaped: a handled card comes back only once its evidence moved, as state "new"
        // carrying `previously` (backend/app/routers/harness.py drops seen cards).
        mk("R2", 2, { previously: { state: "accepted", at: "2026-09-01T00:00:00Z", evidence_changed: true } }),
        mk("R7", 1, { previously: { state: "dismissed", at: "2026-09-01T00:00:00Z", evidence_changed: true } }),
      ] }));
      show();
      await screen.findByTestId("harness-cards");
      fireEvent.change(screen.getByTestId("harness-filter-harness"), { target: { value: "anthropic:opus" } });
      expect(screen.getAllByTestId("harness-card")).toHaveLength(1);
      fireEvent.change(screen.getByTestId("harness-filter-harness"), { target: { value: "" } });
      fireEvent.change(screen.getByTestId("harness-filter-state"), { target: { value: "new" } });
      expect(screen.getAllByTestId("harness-card").map((c) => c.dataset.rule)).toEqual(["R2"]);
      expect(screen.getAllByTestId("harness-card")).toHaveLength(1);
      fireEvent.change(screen.getByTestId("harness-filter-state"), { target: { value: "changed" } });
      expect(screen.getAllByTestId("harness-card")).toHaveLength(2);
      fireEvent.change(screen.getByTestId("harness-filter-state"), { target: { value: "handled" } });
      expect(screen.getAllByTestId("harness-card")).toHaveLength(2);
    });

    it("names the filter when nothing matches", async () => {
      window.localStorage.clear();
      cards.mockResolvedValueOnce(payload({ cards: [mk("R2", 1), mk("R7", 1)] }));
      show();
      await screen.findByTestId("harness-cards");
      fireEvent.click(screen.getAllByTestId("harness-rule-chip").find((c) => c.dataset.rule === "R7")!);
      fireEvent.change(screen.getByTestId("harness-filter-harness"), { target: { value: "gbagent:qwen3.6" } });
      fireEvent.change(screen.getByTestId("harness-filter-state"), { target: { value: "handled" } });
      expect(screen.getByTestId("harness-filter-empty")).toHaveTextContent(
        /^No R2 cards for gbagent:qwen3\.6 previously accepted or dismissed\.$/,
      );
      expect(screen.queryByTestId("harness-no-cards")).toBeNull();
    });

    it("falls back to defaults when storage throws", async () => {
      const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("no"); });
      cards.mockResolvedValueOnce(fixture());
      show();
      await screen.findByTestId("harness-cards");
      expect(screen.getAllByTestId("harness-card")).toHaveLength(2);
      spy.mockRestore();
    });
  });
});
